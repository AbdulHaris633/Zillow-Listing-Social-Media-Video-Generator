"""Zillow listing extraction, with honest failure reporting.

Zillow runs bot protection (PerimeterX/HUMAN) and has no public listing API, so
this module is built to *report* what it got rather than pretend it succeeded.
Fetch backends, cheapest first; "auto" escalates through them:

    curl      - curl_cffi, impersonating Chrome's TLS fingerprint.
    requests  - plain HTTP with a browser-shaped session and warm-up.
    browser   - Playwright Chromium/Chrome, hardened, optionally persistent.
    file      - a page you saved from your own browser. Always works.

Measured against a live listing, every unattended backend was eventually
served HTTP 403: header and TLS hardening raise the odds but do not settle
the matter. What reliably works is `solve_challenge` — a visible browser in
which the operator clears the press-and-hold check by hand once — combined
with `profile_dir`, which persists the resulting cookie so later runs go
through headless and unattended. The challenge gesture is never automated.

Parsing is deliberately shape-agnostic. Zillow reshuffles its Next.js payload
several times a year, so instead of hardcoding a key path we walk the whole
JSON tree (including JSON-encoded strings nested inside it, which is where
`gdpClientCache` lives) and score dicts by how many property-ish keys they have.
A separate DOM parser keyed on data-testid handles pages whose payload has
already been consumed — i.e. anything saved out of a real browser — and is the
only source of per-room photo captions.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import requests
from bs4 import BeautifulSoup

from .models import Listing, Photo, Unit, _as_number, _clean

# A date in either of the two shapes Zillow prints: "8/6/2026" in tables,
# "August 6, 2026" in prose. Month names are listed rather than matched as
# [A-Z][a-z]+ so the pattern stays correct under re.IGNORECASE.
DATE_PATTERN = (
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}"
)

# How long to wait for the price-history table after reaching the bottom of a
# listing. It is fetched separately from the page, and on a sold listing it is
# the only place the sale date exists. Listings without one simply time out,
# so this is also a floor on how much slower every sold fetch becomes.
PRICE_HISTORY_TIMEOUT = 6000  # ms per attempt
PRICE_HISTORY_ATTEMPTS = 3

# Scroll passes, each about one viewport. A listing page runs to tens of
# thousands of pixels; the loop exits early the moment the page stops
# moving, so this is a ceiling for the longest pages rather than a cost
# every fetch pays.
SCROLL_PASSES = 60

# Markers that mean "you were blocked", not "this listing has no data".
BLOCK_MARKERS = (
    "px-captcha",
    "perimeterx",
    "captcha-delivery",
    "please verify you are a human",
    "access to this page has been denied",
    "unusual activity from your computer network",
    "help us keep zillow a safe",
    "/_px/",
)

# A small pool of current desktop Chrome builds. The UA, the sec-ch-ua header
# and (for the curl_cffi backend) the TLS fingerprint are all kept consistent
# with each other — a mismatch between them is itself a detection signal.
CHROME_BUILDS = [
    ("126", "Macintosh; Intel Mac OS X 10_15_7", "macOS", "chrome126"),
    ("127", "Windows NT 10.0; Win64; x64", "Windows", "chrome127"),
    ("128", "Macintosh; Intel Mac OS X 10_15_7", "macOS", "chrome124"),
    ("125", "X11; Linux x86_64", "Linux", "chrome124"),
]


def build_headers(referer: str = "https://www.google.com/", build: tuple | None = None) -> dict:
    """A complete, self-consistent desktop-Chrome header set.

    Header *completeness and order* matter as much as the values: a request
    with a Chrome UA but no sec-ch-ua/sec-fetch family looks nothing like
    Chrome. Python dicts preserve insertion order and requests sends them in
    that order, so these are listed the way a browser sends them.
    """
    version, platform_ua, platform_label, _ = build or random.choice(CHROME_BUILDS)
    return {
        "sec-ch-ua": f'"Chromium";v="{version}", "Not(A:Brand";v="24", "Google Chrome";v="{version}"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform_label}"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": (
            f"Mozilla/5.0 ({platform_ua}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Referer": referer,
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-US,en;q=0.9",
    }


HEADERS = build_headers()

# Zillow will happily serve a few requests and then start refusing. Pacing is
# the cheapest and most effective single measure, especially in batch runs.
MIN_INTERVAL = (4.0, 9.0)
_last_request_at = 0.0


def throttle(interval: tuple[float, float] = MIN_INTERVAL) -> None:
    """Sleep so consecutive fetches are spaced by a randomised interval."""
    global _last_request_at
    wait = random.uniform(*interval) - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()

# Keys that identify Zillow's property object, whatever it's nested inside.
_PROPERTY_KEYS = {
    "streetAddress", "zipcode", "bedrooms", "bathrooms", "livingArea",
    "livingAreaValue", "homeStatus", "zpid", "price", "yearBuilt", "homeType",
}


class ScrapeBlocked(RuntimeError):
    """Raised when Zillow served bot protection instead of the listing."""


@dataclass
class ScrapeResult:
    listing: Listing
    blocked: bool = False
    source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked and not self.listing.missing_required()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def looks_blocked(html: str, status: int = 200) -> bool:
    if status in (403, 429, 503):
        return True
    head = html[:200_000].lower()
    return any(marker in head for marker in BLOCK_MARKERS)


def _warm_up(session, base: str, timeout: int, headers: dict) -> None:
    """Land on the homepage first to pick up cookies, as a browser would.

    A request that arrives at a deep listing URL with an empty cookie jar is
    an obvious tell. Failure here is ignored — it is an optimisation, not a
    prerequisite.
    """
    try:
        session.get(base, timeout=timeout, headers={**headers, "Sec-Fetch-Site": "none", "Referer": ""})
        time.sleep(random.uniform(0.8, 2.0))
    except Exception:  # noqa: BLE001
        pass


def fetch_requests(
    url: str,
    timeout: int = 25,
    *,
    proxy: str | None = None,
    warm_up: bool = True,
    attempts: int = 3,
) -> tuple[str, int]:
    """Plain HTTP with a browser-shaped session, retries and backoff."""
    build = random.choice(CHROME_BUILDS)
    headers = build_headers(build=build)

    session = requests.Session()
    session.headers.update(headers)
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    if warm_up:
        _warm_up(session, "https://www.zillow.com/", timeout, headers)

    html, status = "", 0
    for attempt in range(attempts):
        throttle()
        response = session.get(url, timeout=timeout, allow_redirects=True)
        html, status = response.text, response.status_code
        if not looks_blocked(html, status):
            return html, status
        if attempt < attempts - 1:
            # Exponential backoff with jitter; hammering a block makes it worse.
            time.sleep((2**attempt) * random.uniform(2.0, 4.0))
    return html, status


def fetch_curl(url: str, timeout: int = 25, *, proxy: str | None = None, attempts: int = 3) -> tuple[str, int]:
    """curl_cffi backend — matches Chrome's TLS/HTTP2 fingerprint.

    This is the biggest single upgrade over `requests`. Python's TLS stack has
    a JA3 fingerprint nothing like a browser's, so a request can look perfect
    at the HTTP layer and still be flagged during the handshake. curl_cffi
    impersonates a real Chrome build end to end.
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise RuntimeError(
            "--fetch curl needs curl_cffi:\n    pip install curl_cffi"
        ) from exc

    impersonate = random.choice(CHROME_BUILDS)[3]
    # Deliberately minimal: curl_cffi supplies a full, internally consistent
    # header set matching whichever Chrome it impersonates. Overriding those
    # with our own is how you end up advertising one Chrome version in
    # sec-ch-ua while the TLS handshake fingerprints as another — worse than
    # sending nothing custom at all.
    extra = {"Referer": "https://www.google.com/", "Accept-Language": "en-US,en;q=0.9"}

    html, status = "", 0
    with curl_requests.Session(impersonate=impersonate) as session:
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        try:
            session.get("https://www.zillow.com/", timeout=timeout)
            time.sleep(random.uniform(0.8, 2.0))
        except Exception:  # noqa: BLE001
            pass

        for attempt in range(attempts):
            throttle()
            response = session.get(url, timeout=timeout, headers=extra)
            html, status = response.text, response.status_code
            if not looks_blocked(html, status):
                return html, status
            if attempt < attempts - 1:
                time.sleep((2**attempt) * random.uniform(2.0, 4.0))
    return html, status


# Removes the handful of properties that betray an automated Chrome. Injected
# before any page script runs, so page-level fingerprinting sees the patched
# values rather than the defaults Playwright leaves behind.
STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const query = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : query(p)
);
"""


def _await_human(page, status: int, timeout: int) -> tuple[str, int]:
    """Hold while the operator clears a challenge in the open browser window.

    Polls the page rather than waiting on a keypress, so it continues the
    moment the challenge clears without the operator switching back to the
    terminal.
    """
    print(
        "\n  Zillow is showing a human-verification challenge.\n"
        "  Complete it in the browser window that just opened "
        f"(waiting up to {timeout}s)…"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        try:
            html = page.content()
        except Exception:  # noqa: BLE001 - page navigating mid-poll
            continue
        if not looks_blocked(html, 200):
            print("  Challenge cleared — continuing.\n")
            page.wait_for_timeout(2500)  # let the listing hydrate
            return page.content(), 200

    print("  Timed out waiting for the challenge to be cleared.\n")
    return page.content(), status


def _scroll_to_end(page) -> None:
    """Walk to the bottom of whatever actually scrolls.

    The desktop listing layout puts the page inside a scrolling
    `div.layout-container-desktop` rather than letting the window scroll:
    `document.body.scrollHeight` equals the viewport, `window.scrollY` is
    pinned at 0, and `window.scrollBy` is a no-op. A window-only loop reads
    that motionless scrollY as "already at the bottom" and gives up on the
    first pass, so every section below the fold — price history among them —
    is never approached and never renders.

    So find the real scroller first: the window if it moves, else the tallest
    overflowing element. Height is re-read every pass because these pages grow
    as they load (one ran 6,080 -> 9,126px mid-scroll), and a bottom computed
    once is the wrong bottom.
    """
    try:
        for _ in range(SCROLL_PASSES):
            at_bottom = page.evaluate(
                """() => {
                    const doc = document.scrollingElement || document.documentElement;
                    let el = doc;
                    if (doc.scrollHeight <= doc.clientHeight + 4) {
                        let best = null;
                        for (const e of document.querySelectorAll('div,main,section')) {
                            const s = getComputedStyle(e);
                            if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') continue;
                            if (e.scrollHeight <= e.clientHeight + 200) continue;
                            if (!best || e.scrollHeight > best.scrollHeight) best = e;
                        }
                        if (best) el = best;
                    }
                    const before = el.scrollTop;
                    el.scrollTop = before + el.clientHeight * 0.9;
                    return el.scrollTop === before;
                }"""
            )
            page.wait_for_timeout(random.randint(160, 320))
            if at_bottom:
                break
    except Exception:  # noqa: BLE001 - scrolling is best-effort
        pass


def _await_price_history(page) -> bool:
    """Wait for the price-history rows, nudging the section into view.

    Zillow fetches this table separately once its heading is approached, so
    two things go wrong independently: the section is never looked at, or it
    is looked at and the request has not landed. Scrolling to the bottom
    handles the first; this handles the second.

    The first wait is unconditional — the rows are what matter, and keying
    off the heading would skip the wait entirely on any page that renders
    them together. Retries are spent only when a heading *is* present, so a
    listing with no price history costs one timeout rather than three.
    """
    try:
        if page.query_selector("tr[label]"):
            return True
        for attempt in range(PRICE_HISTORY_ATTEMPTS):
            try:
                page.wait_for_selector("tr[label]", timeout=PRICE_HISTORY_TIMEOUT)
                return True
            except Exception:  # noqa: BLE001 - not there yet, maybe not ever
                pass
            heading = page.query_selector(
                "xpath=//*[self::h2 or self::h3][contains(translate(text(),"
                "'PRICEHISTORY','pricehistory'),'price history')]"
            )
            if heading is None or attempt == PRICE_HISTORY_ATTEMPTS - 1:
                return False
            # Rock the viewport: a section already in view fires no fresh
            # intersection, so leaving and returning re-triggers the load.
            try:
                # Back off using the real scroller, not the window — on the
                # desktop layout `window.scrollBy` moves nothing at all.
                page.evaluate(
                    """() => {
                        const doc = document.scrollingElement || document.documentElement;
                        let el = doc;
                        if (doc.scrollHeight <= doc.clientHeight + 4) {
                            for (const e of document.querySelectorAll('div,main,section')) {
                                const s = getComputedStyle(e);
                                if (s.overflowY !== 'auto' && s.overflowY !== 'scroll') continue;
                                if (e.scrollHeight <= e.clientHeight + 200) continue;
                                if (el === doc || e.scrollHeight > el.scrollHeight) el = e;
                            }
                        }
                        el.scrollTop -= 1200;
                    }"""
                )
                page.wait_for_timeout(300)
                heading.scroll_into_view_if_needed(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001 - never let this sink a fetch
        pass
    return False


def fetch_browser(
    url: str,
    timeout: int = 45,
    headless: bool = True,
    *,
    proxy: str | None = None,
    profile_dir: str | Path | None = None,
    channel: str | None = None,
    solve_challenge: bool = False,
    challenge_timeout: int = 180,
) -> tuple[str, int]:
    """Playwright Chromium, hardened. Optional dependency.

    `solve_challenge` opens the window and waits for *you* to clear Zillow's
    press-and-hold check by hand, then carries on. Paired with `profile_dir`
    the resulting cookie is saved, so later runs usually skip the challenge
    entirely. The gesture is never automated — that is the whole point of the
    check, and a scripted press is both a bad-faith answer to it and the first
    thing their detection looks for.

    `channel="chrome"` drives the real Google Chrome installed on the machine
    instead of Playwright's bundled Chromium build, which is both a distinct
    binary and (when installed as the headless shell) trivially identifiable.

    `profile_dir` keeps cookies between runs, which matters more than any
    single fingerprint tweak: a session that has been seen before is trusted
    far more than a cold one. Non-headless gets through noticeably more often
    than headless.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "--fetch browser needs Playwright:\n"
            "    pip install playwright && playwright install chromium"
        ) from exc

    build = random.choice(CHROME_BUILDS)
    headers = build_headers(build=build)
    launch_args = [
        "--disable-blink-features=AutomationControlled",  # drops the automation flag
        "--disable-features=IsolateOrigins,site-per-process",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    context_args = dict(
        user_agent=headers["User-Agent"],
        viewport={"width": random.choice([1440, 1512, 1680]), "height": random.choice([816, 900, 945])},
        locale="en-US",
        timezone_id="America/Chicago",
        extra_http_headers={"Accept-Language": headers["Accept-Language"]},
    )
    if proxy:
        context_args["proxy"] = {"server": proxy}

    throttle()
    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless, "args": launch_args}
        if channel:
            launch_kwargs["channel"] = channel
        if profile_dir:
            context = pw.chromium.launch_persistent_context(
                str(Path(profile_dir).expanduser()), **launch_kwargs, **context_args
            )
            browser = None
        else:
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(**context_args)

        context.add_init_script(STEALTH_INIT)
        page = context.new_page()
        try:
            response = page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
            status = response.status if response else 0
            # Behave like a reader: pause, scroll, pause. Zillow lazy-loads the
            # gallery, so this also materialises photos the DOM parser wants.
            page.wait_for_timeout(random.randint(1200, 2200))
            # Far enough to reach the bottom of a long building page. Zillow
            # renders sections as they approach the viewport, and the agent
            # block sits below the units table, the description and the facts
            # list — three scrolls left it unrendered, so rentals came back
            # with no management company at all.
            for _ in range(8):
                page.mouse.wheel(0, random.randint(900, 1600))
                page.wait_for_timeout(random.randint(400, 900))
            # Then walk the rest of the way to the bottom. A fixed number of
            # wheel ticks covers a fixed distance, and these pages are not a
            # fixed length — price history sits below the Zestimate chart, the
            # comps carousel and the whole facts list, and was still
            # unrendered when the HTML was read.
            _scroll_to_end(page)

            # Price history is fetched separately once its section is
            # approached, so reaching the bottom is not enough — the request
            # has to come back too. A sold page carries `canShowPriceHistory`
            # in its payload but none of the rows, and the sale date lives
            # nowhere else, so it is worth waiting for explicitly.
            _await_price_history(page)

            page.wait_for_timeout(random.randint(800, 1500))
            html = page.content()

            if solve_challenge and looks_blocked(html, status):
                html, status = _await_human(page, status, challenge_timeout)
        finally:
            context.close()
            if browser:
                browser.close()
    return html, status


def fetch(
    url: str,
    backend: str = "auto",
    timeout: int = 25,
    headless: bool = True,
    *,
    proxy: str | None = None,
    profile_dir: str | Path | None = None,
    channel: str | None = None,
    solve_challenge: bool = False,
) -> tuple[str, int]:
    """Fetch a page, escalating through backends when `backend` is "auto".

    Cheap-to-expensive: curl_cffi (if installed) -> requests -> browser (if
    installed). Each rung costs more and gets through more often, so there is
    no reason to start at the top.
    """
    if backend == "curl":
        return fetch_curl(url, timeout=timeout, proxy=proxy)
    if backend == "requests":
        return fetch_requests(url, timeout=timeout, proxy=proxy)
    if backend == "browser":
        return fetch_browser(url, timeout=timeout, headless=headless, proxy=proxy,
                             profile_dir=profile_dir, channel=channel,
                             solve_challenge=solve_challenge)

    html, status = "", 0
    for step in ("curl", "requests", "browser"):
        try:
            html, status = fetch(
                url, backend=step, timeout=timeout, headless=headless,
                proxy=proxy, profile_dir=profile_dir, channel=channel,
                solve_challenge=solve_challenge,
            )
        except RuntimeError:
            continue  # backend not installed — try the next rung
        if not looks_blocked(html, status):
            return html, status
    return html, status


# --------------------------------------------------------------------------
# JSON tree walking
# --------------------------------------------------------------------------

def _walk(node: Any, depth: int = 0, budget: list[int] | None = None) -> Iterator[dict]:
    """Yield every dict in a JSON tree, descending into JSON-encoded strings.

    Zillow stores the interesting payload (`gdpClientCache`) as a *string* of
    JSON inside the page's JSON. The string budget stops a pathological page
    from turning this into a parsing marathon.
    """
    if budget is None:
        budget = [400]
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1, budget)
    elif isinstance(node, list):
        for item in node[:500]:
            yield from _walk(item, depth + 1, budget)
    elif isinstance(node, str):
        stripped = node.lstrip()
        if budget[0] > 0 and len(node) > 120 and stripped[:1] in "{[":
            budget[0] -= 1
            try:
                yield from _walk(json.loads(node), depth + 1, budget)
            except (ValueError, RecursionError):
                pass


def _find_property(payload: Any) -> dict | None:
    """Pick the dict that looks most like Zillow's property record."""
    best, best_score = None, 0
    for candidate in _walk(payload):
        score = len(_PROPERTY_KEYS & candidate.keys())
        # Prefer nodes that actually carry photos when scores tie.
        if score and any(k in candidate for k in ("responsivePhotos", "originalPhotos", "hugePhotos")):
            score += 1
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 3 else None


def _best_photo_url(entry: Any) -> tuple[str, int]:
    """Highest-resolution JPEG from a Zillow photo entry."""
    if isinstance(entry, str):
        return entry, 0
    if not isinstance(entry, dict):
        return "", 0

    best_url, best_width = "", -1
    sources = entry.get("mixedSources") or entry.get("sources") or {}
    if isinstance(sources, dict):
        # Prefer jpeg: universally decodable by Pillow without extra plugins.
        for key in ("jpeg", "webp"):
            for item in sources.get(key) or []:
                if not isinstance(item, dict):
                    continue
                width = int(item.get("width") or 0)
                if item.get("url") and width > best_width:
                    best_url, best_width = item["url"], width
            if best_url:
                break
    if not best_url:
        for key in ("url", "src", "highResolutionUrl", "jpegUrl"):
            if entry.get(key):
                best_url = entry[key]
                break
    return best_url, max(best_width, 0)


def _photos_from_property(prop: dict, limit: int = 60) -> list[Photo]:
    for key in ("responsivePhotos", "originalPhotos", "hugePhotos", "photos", "galleryPhotos"):
        raw = prop.get(key)
        if not isinstance(raw, list) or not raw:
            continue
        photos: list[Photo] = []
        seen: set[str] = set()
        for entry in raw[:limit]:
            url, width = _best_photo_url(entry)
            if not url or url in seen:
                continue
            seen.add(url)
            caption = ""
            if isinstance(entry, dict):
                caption = _clean(entry.get("caption") or entry.get("subjectType") or "")
            photos.append(Photo(url=url, caption=caption, width=width))
        if photos:
            return photos
    return []


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------

def parse_next_data(soup: BeautifulSoup) -> tuple[Listing | None, str]:
    """Zillow's Next.js payload — the richest source when it's present."""
    payloads: list[Any] = []
    for tag in soup.find_all("script"):
        text = tag.string or tag.get_text() or ""
        script_id = tag.get("id") or ""
        script_type = tag.get("type") or ""
        if script_id == "__NEXT_DATA__" or script_type == "application/json":
            try:
                payloads.append(json.loads(text))
            except ValueError:
                continue
        elif "gdpClientCache" in text or '"zpid"' in text:
            # Hydration blobs assigned to a JS variable rather than a JSON tag.
            match = re.search(r"(\{.*\})", text, re.DOTALL)
            if match:
                try:
                    payloads.append(json.loads(match.group(1)))
                except ValueError:
                    continue

    for payload in payloads:
        prop = _find_property(payload)
        if not prop:
            continue
        address = prop.get("address") if isinstance(prop.get("address"), dict) else {}
        attribution = prop.get("attributionInfo") if isinstance(prop.get("attributionInfo"), dict) else {}
        listing = Listing.from_dict(
            {
                "street": prop.get("streetAddress") or address.get("streetAddress"),
                "city": prop.get("city") or address.get("city"),
                "state": prop.get("state") or address.get("state"),
                "zipcode": prop.get("zipcode") or address.get("zipcode"),
                "price": prop.get("price") or prop.get("listPrice") or prop.get("unformattedPrice"),
                "beds": prop.get("bedrooms"),
                "baths": prop.get("bathrooms"),
                "sqft": prop.get("livingArea") or prop.get("livingAreaValue"),
                "year_built": prop.get("yearBuilt"),
                "home_type": prop.get("homeType") or prop.get("propertyTypeDimension"),
                "status": prop.get("homeStatus"),
                "description": prop.get("description"),
                "agent_name": attribution.get("agentName"),
                "agent_phone": attribution.get("agentPhoneNumber"),
                "brokerage": attribution.get("brokerName"),
                # Sold listings: Zillow names the two sides "buyer" and the
                # listing agent above. lastSoldPrice is the only price a sold
                # page carries once `price` has been zeroed out.
                "buyer_agent": attribution.get("buyerAgentName"),
                "buyer_brokerage": attribution.get("buyerBrokerageName"),
                "sold_date": prop.get("dateSoldString") or prop.get("dateSold"),
                "lot_size": prop.get("lotAreaValue") and
                f"{prop.get('lotAreaValue')} {prop.get('lotAreaUnits') or ''}".strip(),
            }
        )
        listing.photos = _photos_from_property(prop)
        if listing.street or listing.price:
            return listing, "next-data"
    return None, ""


def parse_json_ld(soup: BeautifulSoup) -> tuple[Listing | None, str]:
    """schema.org markup. Thinner than the Next payload but often survives."""
    wanted = {"singlefamilyresidence", "residence", "house", "apartment", "place", "product", "offer"}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
        except ValueError:
            continue
        for node in _walk(payload):
            node_type = str(node.get("@type", "")).lower()
            if node_type not in wanted:
                continue
            address = node.get("address") if isinstance(node.get("address"), dict) else {}
            offers = node.get("offers") if isinstance(node.get("offers"), dict) else {}
            images = node.get("image")
            if isinstance(images, str):
                images = [images]
            listing = Listing.from_dict(
                {
                    "street": address.get("streetAddress"),
                    "city": address.get("addressLocality"),
                    "state": address.get("addressRegion"),
                    "zipcode": address.get("postalCode"),
                    "price": offers.get("price") or node.get("price"),
                    "beds": node.get("numberOfRooms") or node.get("numberOfBedrooms"),
                    "sqft": (node.get("floorSize") or {}).get("value")
                    if isinstance(node.get("floorSize"), dict) else None,
                    "description": node.get("description"),
                    "photos": images or [],
                }
            )
            if listing.street or listing.price:
                return listing, "json-ld"
    return None, ""


def _largest_from_srcset(srcset: str) -> tuple[str, int]:
    """Pick the widest candidate out of a srcset attribute."""
    best_url, best_width = "", -1
    for candidate in srcset.split(","):
        parts = candidate.strip().split()
        if not parts:
            continue
        url = parts[0]
        width = 0
        if len(parts) > 1 and parts[1].endswith("w"):
            try:
                width = int(parts[1][:-1])
            except ValueError:
                width = 0
        if width > best_width:
            best_url, best_width = url, width
    return best_url, max(best_width, 0)


def _photos_from_dom(soup: BeautifulSoup) -> list[Photo]:
    """Gallery images, captioned by the room heading they sit under.

    Zillow's rendered page groups photos into per-room sections, which is a
    better source of captions than anything in the JSON payload — it is where
    'Kitchen' and 'Primary Bedroom' come from.
    """
    photos: list[Photo] = []
    seen: set[str] = set()

    def add(picture, caption: str) -> None:
        # Prefer a jpeg source; Pillow decodes it without extra plugins.
        source = picture.find("source", attrs={"type": "image/jpeg"}) or picture.find("source")
        url, width = ("", 0)
        if source and source.get("srcset"):
            url, width = _largest_from_srcset(source["srcset"])
        if not url:
            img = picture.find("img")
            url = (img.get("src") or "") if img else ""
        if not url:
            return
        # Zillow serves one photo at many sizes; the hash before "-sc_"
        # identifies the underlying image.
        key = re.split(r"-(?:sc|cc|p)_", url.split("/")[-1])[0]
        if key in seen:
            return
        seen.add(key)
        photos.append(Photo(url=url, caption=caption, width=width))

    for room in soup.select('[data-testid="group-by-room-room"]'):
        heading = room.find(["h2", "h3"])
        caption = _clean(heading.get("title") or heading.get_text() if heading else "")
        for picture in room.find_all("picture"):
            add(picture, caption)

    if not photos:  # ungrouped gallery
        for picture in soup.find_all("picture"):
            add(picture, "")
    return photos


# Photos belonging to *other* properties. A building page ends with a "Nearby
# apartments for rent" carousel whose cards carry full-size photos; scooping
# those up would put a competitor's house in the middle of the slideshow.
FOREIGN_PHOTO_ANCESTORS = (
    "[data-test-id='mini-list-card-container']",
    "[data-c11n-component='PropertyCard.Root']",
    "[data-testid='listing-agent-container']",  # the management company's logo
)


def _photos_from_building_dom(soup: BeautifulSoup) -> list[Photo]:
    """Gallery images on a building page, minus everything that isn't this building.

    Building pages use plain <img> rather than the <picture>/srcset markup the
    homedetails gallery uses, so there is no width to compare — the served URL
    is taken as-is.
    """
    foreign: set[int] = set()
    for selector in FOREIGN_PHOTO_ANCESTORS:
        for block in soup.select(selector):
            for image in block.find_all("img"):
                foreign.add(id(image))

    photos: list[Photo] = []
    seen: set[str] = set()
    for image in soup.find_all("img"):
        if id(image) in foreign:
            continue
        url = image.get("src") or ""
        if "photos.zillowstatic.com" not in url:
            continue
        key = photo_key(url)
        if key in seen:
            continue
        seen.add(key)
        photos.append(Photo(url=url, caption=_clean(image.get("alt") or "")))
    return photos


def _span(values: list[float], fmt) -> str:
    """'1' when every unit agrees, '1-2' when they don't. '' when empty."""
    if not values:
        return ""
    low, high = min(values), max(values)
    return fmt(low) if low == high else f"{fmt(low)}-{fmt(high)}"


def _units_from_building_dom(soup: BeautifulSoup) -> dict[str, Any]:
    """Summarise the whole availability table, not one row of it.

    A building has no single price, bed count or floor area — it has a table of
    units. Quoting the cheapest row alone undersells a building that also has
    larger units: Tiger Village would read "1 bd · 513 sq ft · $1,012/mo" when
    Zillow itself headlines it "1-2 beds" and "$1,012 - $1,304". So each stat
    becomes a range whenever the units disagree.

    The numeric fields keep the low end, because they are what the required-
    field gate and any sorting look at; the *_text fields carry the range and
    win on screen.
    """
    units: list[Unit] = []
    hedged = False  # Zillow's "+" — fees may push the quoted rent higher

    for row in soup.select("[data-test-id='unit-table-row']"):
        cells = row.find_all("td")
        text = _clean(row.get_text(" "))

        money = re.search(r"\$[\d,]+\+?", text)
        if not money:
            continue
        rent = _as_number(money.group(0))
        if rent is None:
            continue
        hedged = hedged or money.group(0).endswith("+")

        unit = Unit(rent=rent)
        # The unit's own label is the first cell's leading text: a number
        # ("604") in a large building, a layout name ("2 Bedroom") in a small
        # one. Taken before the layout paragraph so the two don't run together.
        if cells and (label := cells[0].find(["span", "div"])):
            unit.name = _clean(label.get_text())
        if match := re.search(r"([\d.]+)\s*bd\b", text):
            unit.beds = _as_number(match.group(1))
        if match := re.search(r"([\d.]+)\s*ba\b", text):
            unit.baths = _as_number(match.group(1))
        # Sqft is a bare number in its own column. A regex over the row text
        # would match the rent's digits just as happily, so read the cell.
        if len(cells) >= 2 and re.fullmatch(r"[\d,]+", _clean(cells[1].get_text())):
            unit.sqft = _as_number(_clean(cells[1].get_text()))
        if len(cells) >= 3:
            unit.available = _clean(cells[2].get_text())
        units.append(unit)

    if not units:
        return {}

    units.sort(key=lambda u: (u.rent if u.rent is not None else 0, u.name))
    rents = [u.rent for u in units if u.rent is not None]
    beds = [u.beds for u in units if u.beds is not None]
    baths = [u.baths for u in units if u.baths is not None]
    sqfts = [u.sqft for u in units if u.sqft is not None]

    money_fmt = lambda v: f"${int(round(v)):,}"  # noqa: E731
    count_fmt = lambda v: f"{v:g}"  # noqa: E731 - 1.0 -> "1", 1.5 -> "1.5"

    data: dict[str, Any] = {
        "units": [u.to_dict() for u in units],
        "price": min(rents),
        "price_text": f"{_span(rents, money_fmt)}{'+' if hedged else ''}/mo",
    }
    if beds:
        data["beds"], data["beds_text"] = min(beds), _span(beds, count_fmt)
    if baths:
        data["baths"], data["baths_text"] = min(baths), _span(baths, count_fmt)
    if sqfts:
        data["sqft"], data["sqft_text"] = min(sqfts), _span(sqfts, lambda v: f"{int(v):,}")
    return data


def parse_building_dom(soup: BeautifulSoup) -> tuple[Listing | None, str]:
    """Apartment and building pages (zillow.com/apartments/...).

    A different page type from a for-sale home, and a different vintage of
    markup: it mixes `data-test-id` (hyphenated) with `data-testid`, and keeps
    unhashed legacy classes on the agent block. Crucially its <h1> is the
    *building name*, not the address — parse_dom's heading fallback reads that
    as the street, which is why these pages came back with a name where the
    address should be and nothing else at all.
    """
    title = soup.select_one("[data-test-id='bdp-building-title']")
    address = soup.select_one("[data-test-id='bdp-building-address']")
    if not (title or address):
        return None, ""

    data: dict[str, Any] = {}
    if address:
        data["address"] = _clean(address.get_text())
    data.update(_units_from_building_dom(soup))

    # Rentals are listed by a management company, in a block that predates the
    # data-testid convention and still uses stable, unhashed class names.
    if agent := soup.select_one(".ds-listing-agent-display-name"):
        data["agent_name"] = _clean(agent.get_text())
    # The display name is often a role ("Leasing Agent") rather than a person,
    # which makes the company beside it the useful half of the attribution.
    if company := soup.select_one(".ds-listing-agent-business-name"):
        if text := _clean(company.get_text()):
            data["brokerage"] = text
    for line in soup.select(".ds-listing-agent-info-text"):
        text = _clean(line.get_text())
        if re.search(r"\d{3}[-.\s]?\d{3,4}", text):
            data.setdefault("agent_phone", text)
            break

    # "What's special" is a heading followed by an <article>; the copy carries
    # only hashed classes, so anchor on the heading and walk forward.
    for heading in soup.find_all(["h2", "h3"]):
        if not re.match(r"what'?.?s special", _clean(heading.get_text()), re.I):
            continue
        article = heading.find_next("article")
        if not article:
            continue
        # Drop the "Show more" button so its label doesn't land in the copy.
        for button in article.find_all("button"):
            button.decompose()
        data["description"] = _clean(article.get_text(" "))
        break

    for chip in soup.select("span.c11n-semantic"):
        text = _clean(chip.get_text())
        if re.fullmatch(r"(Apartment|Condo|Townhouse|House|Multi[- ]family)\b.*", text, re.I):
            data.setdefault("home_type", text)
            break

    data.setdefault("status", "FOR_RENT")

    listing = Listing.from_dict(data)
    listing.photos = _photos_from_building_dom(soup)

    if not (listing.street or listing.price or listing.photos):
        return None, ""
    return listing, "building"


def _parse_date(text: str):
    """'8/11/2026' or 'August 6, 2026' to a date, or None if neither."""
    from datetime import datetime

    text = _clean(text).replace(",", "")
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _sale_from_price_history(soup: BeautifulSoup) -> dict[str, Any]:
    """The most recent Sold row from the price-history table.

    Each row carries a `label` attribute summarising itself —
    "Date: 8/11/2026, Event: Sold, Price: $600,000 (+41.2%)" — which is far
    steadier than reading the cells, and distinguishes a sale from the
    "Listing removed" and "Listed for rent" rows sitting beside it.

    Rows are picked by date rather than by position. Zillow happens to sort
    newest-first today, but a listing's *latest* sale is what a "just sold"
    post is about, and that should not rest on the sort order holding.
    """
    best_date = None
    best: dict[str, Any] = {}

    for row in soup.select("tr[label]"):
        label = _clean(row.get("label"))
        if not re.search(r"Event:\s*Sold\b", label, re.I):
            continue
        date_match = re.search(r"Date:\s*([^,]+)", label)
        if not date_match:
            continue
        parsed = _parse_date(date_match.group(1))
        if parsed is None or (best_date is not None and parsed <= best_date):
            continue
        best_date = parsed
        best = {"sold_date": _clean(date_match.group(1))}
        # A sold page often shows "Price Unknown" in its header while the
        # history still carries the figure, so this is worth keeping.
        if price := re.search(r"Price:\s*(\$[\d,]+)", label):
            best["sold_price_text"] = price.group(1)

    return best


def parse_dom(soup: BeautifulSoup) -> tuple[Listing | None, str]:
    """Read the rendered page via its data-testid hooks.

    This is the parser that works on HTML saved from a browser, where the
    hydration payload has already been consumed and discarded. Zillow's CSS
    class names are hashed and change constantly, so every selector here keys
    off data-testid or semantic structure instead.
    """
    data: dict[str, Any] = {}

    heading = soup.find("h1")
    if heading:
        data["address"] = _clean(heading.get_text())

    price = soup.select_one('[class*="price-text"]') or soup.select_one('[data-testid="price"]')
    if price:
        data["price"] = _clean(price.get_text())

    # Beds/baths/sqft appear twice (mobile + desktop blocks); first wins.
    for container in soup.select('[data-testid="bed-bath-sqft-text__container"]'):
        value = container.select_one('[data-testid="bed-bath-sqft-text__value"]')
        label = container.select_one('[data-testid="bed-bath-sqft-text__description"]')
        if not value or not label:
            continue
        key = _clean(label.get_text()).lower().rstrip("s")
        field = {"bed": "beds", "bath": "baths", "sqft": "sqft"}.get(key)
        if field and field not in data:
            data[field] = _clean(value.get_text())

    # Off-market and sold pages use a newer, simpler container: two bare spans,
    # value then label, with no per-part testids to key on.
    for container in soup.select('[data-testid="bed-bath-sqft-fact-container"]'):
        spans = container.find_all("span", recursive=False) or container.find_all("span")
        if len(spans) < 2:
            continue
        key = _clean(spans[1].get_text()).lower().rstrip("s")
        field = {"bed": "beds", "bath": "baths", "sqft": "sqft"}.get(key)
        if field and field not in data:
            data[field] = _clean(spans[0].get_text())

    status = soup.select_one('[data-testid="home-status"]')
    if status:
        data["status"] = _clean(status.get_text()).upper().replace(" ", "_")

    description = soup.select_one('[data-testid="property-description"]')
    if description:
        visible = _clean(description.get_text())
        # The visible copy is truncated with an ellipsis; the full text rides
        # along on the "Show more" button's `text` attribute. That button is
        # searched for document-wide rather than inside the description: the
        # markup nests a <div> inside the <p>, and HTML parsers close the <p>
        # early, leaving the button as a sibling rather than a descendant.
        full = max(
            (_clean(b["text"]) for b in soup.find_all("button", attrs={"text": True})),
            key=len,
            default="",
        )
        data["description"] = full if len(full) > len(visible) else visible

    agent = soup.select_one('[data-testid="attribution-LISTING_AGENT"]')
    if agent:
        parts = [_clean(s.get_text()) for s in agent.find_all("span")] or [_clean(agent.get_text())]
        for part in parts:
            stripped = part.rstrip(",").strip()
            if re.search(r"\d{3}[-.\s]?\d{3,4}", stripped):
                data.setdefault("agent_phone", stripped)
            elif stripped:
                data.setdefault("agent_name", stripped)

    broker = soup.select_one('[data-testid="attribution-BROKER"]')
    if broker:
        for part in [_clean(s.get_text()) for s in broker.find_all("span")]:
            if part and not re.search(r"\d{3}[-.\s]?\d{3,4}", part):
                data.setdefault("brokerage", part.rstrip(",").strip())
                break

    # Fall back to the agent chip at the top of the page.
    chip = soup.select_one('[data-testid="agent-section__container"]')
    if chip:
        lines = [_clean(s.get_text()) for s in chip.find_all("span") if _clean(s.get_text())]
        if lines:
            data.setdefault("agent_name", lines[0])
        if len(lines) > 1:
            data.setdefault("brokerage", lines[1])

    # "Sold 08/06/2026" in the price-history table, or the same fact spelled
    # out in the listing details. Sold pages put it in one place or the other
    # depending on how much history the MLS released.
    data.update(_sale_from_price_history(soup))

    if "sold_date" not in data:
        for row in soup.select("tr, li, span, p"):
            text = _clean(row.get_text(" "))
            # Short rows only: a whole page of text mentions "sold" somewhere
            # and would pair it with the first unrelated date on the page.
            if len(text) > 80 or "sold" not in text.lower():
                continue
            # The date sits either side of the word depending on the layout —
            # a price-history row leads with it, a sentence trails it — so
            # match the date on its own rather than anchoring to "sold".
            if match := re.search(DATE_PATTERN, text):
                data["sold_date"] = match.group(0)
                break

    # Zillow's own "Bought with:" block, which carries no testid of its own —
    # only the label beside it identifies the buyer's side of the sale.
    for label in soup.find_all(["span", "p"]):
        if not re.fullmatch(r"bought with:?", _clean(label.get_text()), re.I):
            continue
        block = label.find_next_sibling(["div", "p"]) or (
            label.parent.find_next_sibling("div") if label.parent else None
        )
        if not block:
            continue
        lines = [_clean(p.get_text()) for p in block.find_all("p")] or [
            _clean(block.get_text())
        ]
        lines = [line for line in lines if line]
        if lines:
            # "Shawna Neuner, 2018019696" — the licence number is not a name.
            data.setdefault("buyer_agent", re.split(r",\s*\d", lines[0])[0].strip(" ,"))
        if len(lines) > 1:
            data.setdefault("buyer_brokerage", lines[1])
        break

    # "Single family residence", "Built in 1979", "0.61 Acres" chips.
    glance = soup.select_one('[data-testid="at-a-glance"]') or soup.select_one(
        '[aria-label="At a glance facts"]'
    )
    if glance:
        for span in glance.find_all("span"):
            text = _clean(span.get_text())
            if not text:
                continue
            if match := re.match(r"Built in (\d{4})", text, re.I):
                data.setdefault("year_built", match.group(1))
            # "0.61 Acres", "6,534 sqft lot", "6,534 Square Feet Lot" — the
            # wording varies by page vintage, the leading number does not.
            elif re.search(
                r"\b(acres?|sq ?ft lot|square (feet|foot) lot)\b", text, re.I
            ) and re.match(r"[\d.]", text):
                data.setdefault("lot_size", text)
            elif re.search(r"(residence|house|condo|townhouse|apartment|land)", text, re.I):
                data.setdefault("home_type", text)

    # A sold page's header reads "Price Unknown" wherever the sale price was
    # never released, but the history row often still carries the figure.
    sold_price = data.pop("sold_price_text", "")
    if sold_price and _as_number(data.get("price")) is None:
        data["price"] = sold_price

    listing = Listing.from_dict(data)
    listing.photos = _photos_from_dom(soup)

    if not (listing.street or listing.price or listing.photos):
        return None, ""
    return listing, "dom"


def parse_meta(soup: BeautifulSoup) -> tuple[Listing | None, str]:
    """Last resort: OpenGraph tags. Usually just address + one photo."""
    def meta(prop: str) -> str:
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return _clean(tag.get("content")) if tag and tag.get("content") else ""

    title = meta("og:title") or _clean(soup.title.string if soup.title else "")
    if not title:
        return None, ""
    # og:title is typically "123 Main St, City, ST 12345 | MLS #123 | Zillow"
    address = title.split("|")[0].strip()
    data: dict[str, Any] = {"address": address, "description": meta("og:description")}
    image = meta("og:image")
    if image:
        data["photos"] = [image]
    price = re.search(r"\$[\d,]{4,}", meta("og:description") or title)
    if price:
        data["price"] = price.group(0)
    listing = Listing.from_dict(data)
    return (listing, "og-meta") if listing.street else (None, "")


def photo_key(url: str) -> str:
    """Identity of the underlying image, independent of the size served."""
    return re.split(r"-(?:sc|cc|p)_", url.split("/")[-1])[0]


def parse_html(html: str) -> ScrapeResult:
    """Run every parser and keep the richest result."""
    soup = BeautifulSoup(html, "lxml")
    notes: list[str] = []
    best: Listing | None = None
    best_source = ""
    captioned_photos: list[Photo] = []

    # Richest first: later parsers only fill in what earlier ones missed.
    # parse_building_dom outranks parse_dom because on a building page the two
    # disagree about the address, and parse_dom is the one that is wrong — it
    # reads the <h1> building name as the street.
    for parser in (parse_next_data, parse_building_dom, parse_dom, parse_json_ld, parse_meta):
        try:
            listing, source = parser(soup)
        except Exception as exc:  # noqa: BLE001 - one bad parser must not sink the run
            notes.append(f"{parser.__name__} failed: {exc}")
            continue
        if not listing:
            continue
        # Captions are collected from every parser, not just the winner: the
        # JSON payload carries the full gallery but usually no captions, while
        # the rendered DOM carries a captioned subset. Keyed on image identity
        # so the two can be reconciled across different served sizes.
        if not captioned_photos:
            captioned_photos = [p for p in listing.photos if p.caption and p.url]
        if best is None:
            best, best_source = listing, source
        else:
            # Later parsers only fill gaps; the first (richest) source wins.
            best = listing.merged_with(best)
            best_source = f"{best_source}+{source}"

    if best is not None and captioned_photos:
        gallery_keys = {photo_key(p.url) for p in best.photos if p.url}

        # Where the same image appears in both, just copy the caption over.
        matched = 0
        by_key = {photo_key(p.url): p.caption for p in captioned_photos if p.url}
        for photo in best.photos:
            if not photo.caption and photo.url and by_key.get(photo_key(photo.url)):
                photo.caption = by_key[photo_key(photo.url)]
                matched += 1

        # Zillow's room-grouped photos are often a separately curated set with
        # no hash overlap with the main gallery. Those are the only captioned
        # images available, and they are the most presentable shots of each
        # room, so they lead the slideshow and the gallery follows.
        extra = [p for p in captioned_photos if p.url and photo_key(p.url) not in gallery_keys]
        if extra:
            best.photos = extra + best.photos
            notes.append(f"led with {len(extra)} captioned room photo(s) ahead of the gallery")
        if matched:
            notes.append(f"applied {matched} room caption(s) to gallery photos")

    if best is None:
        return ScrapeResult(Listing(), blocked=False, source="", notes=notes + ["no listing data found in page"])

    best.source = best_source
    best.notes = notes
    return ScrapeResult(best, source=best_source, notes=notes)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def scrape(
    url: str = "",
    *,
    html_file: str | Path | None = None,
    backend: str = "auto",
    timeout: int = 25,
    headless: bool = True,
    proxy: str | None = None,
    profile_dir: str | Path | None = None,
    channel: str | None = None,
    solve_challenge: bool = False,
    save_html: str | Path | None = None,
) -> ScrapeResult:
    """Fetch and parse a listing. Never raises on a block — reports it.

    `save_html` writes exactly what was fetched. When a field comes back empty
    the question is always the same — was it absent from the page, or present
    and mis-parsed? — and that is unanswerable without the bytes.
    """
    if html_file:
        html = Path(html_file).expanduser().read_text(encoding="utf-8", errors="replace")
        status = 200
        source_note = f"saved HTML ({Path(html_file).name})"
    else:
        if not url:
            raise ValueError("scrape() needs either a url or an html_file")
        html, status = fetch(
            url, backend=backend, timeout=timeout, headless=headless,
            proxy=proxy, profile_dir=profile_dir, channel=channel,
            solve_challenge=solve_challenge,
        )
        source_note = f"{backend} HTTP {status}"

    if save_html:
        target = Path(save_html).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        print(f"  Saved the fetched HTML to {target} ({len(html):,} bytes)")

    if looks_blocked(html, status):
        return ScrapeResult(
            Listing(url=url),
            blocked=True,
            source=source_note,
            notes=[f"Zillow bot protection triggered ({source_note})"],
        )

    result = parse_html(html)
    result.listing.url = url or result.listing.url
    result.notes.insert(0, source_note)
    if not result.listing.missing_required():
        return result

    # Data-poor but not blocked: a soft block looks like this too.
    if not result.listing.street and not result.listing.photos:
        result.notes.append("page loaded but contained no listing data (possible soft block)")
    return result
