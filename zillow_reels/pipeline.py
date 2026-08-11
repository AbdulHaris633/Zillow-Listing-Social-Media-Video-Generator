"""End-to-end orchestration: acquire -> photos -> video -> Drive.

The acquisition step is the interesting one. It tries the automated paths in
order, merges whatever each returns, and only involves the human for the fields
that are still missing — a half-successful scrape should mean typing two fields,
not twelve.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import manual as manual_mod
from . import scrape as scrape_mod
from .config import Config
from .models import Listing
from .photos import download_photos
from .video import build_video


@dataclass
class RunOptions:
    url: str = ""
    manual_path: str = ""
    html_file: str = ""
    fetch_backend: str = "auto"
    proxy: str = ""
    profile_dir: str = ""
    channel: str = ""
    solve_challenge: bool = False
    review: bool = False
    # A caller that intends to retry (the ./reel wrapper does) suppresses the
    # fallback template, so a stale one isn't left behind by an attempt that
    # was never meant to be the last word.
    write_fallback_template: bool = True
    # Narrowed for sold listings, which have no asking price and, in
    # non-disclosure states, no published sale price to fall back on.
    required: tuple[str, ...] = ("address", "price", "photos")
    interactive: bool = False
    workdir: Path = Path("out")
    upload: bool = True
    client_secrets: str = ""
    parent_folder_id: str = ""
    console_auth: bool = False
    skip_video: bool = False
    headless: bool = True
    verbose: bool = True
    # Field-level overrides applied on top of whatever was scraped or loaded —
    # this is how extra CSV columns patch a listing the scrape got wrong.
    overrides: Listing | None = None


@dataclass
class RunResult:
    listing: Listing
    video_path: Path | None = None
    photo_paths: list[Path] = field(default_factory=list)
    drive: dict = field(default_factory=dict)
    template_path: Path | None = None
    status: str = "ok"          # ok | needs_input | error
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def acquire(options: RunOptions, cfg: Config) -> tuple[Listing, list[str], bool]:
    """Get the best listing we can from the sources available.

    The third return value is True when Zillow served a challenge and no other
    source made up for it — the caller needs it to tell "the scrape came back
    thin" (worth reviewing) from "the scrape never happened" (worth retrying
    with a visible browser).
    """
    messages: list[str] = []
    listing = Listing(url=options.url)
    blocked = False

    if options.url or options.html_file:
        result = scrape_mod.scrape(
            options.url,
            html_file=options.html_file or None,
            backend=options.fetch_backend,
            headless=options.headless,
            proxy=options.proxy or None,
            profile_dir=options.profile_dir or None,
            channel=options.channel or None,
            solve_challenge=options.solve_challenge,
        )
        messages.extend(result.notes)
        if result.blocked:
            blocked = True
            messages.append("Blocked by Zillow — no listing data came back.")
        elif result.listing.address or result.listing.photos:
            got = [n for n in ("address", "price", "beds", "baths", "sqft", "description", "agent_name")
                   if getattr(result.listing, n if n != "price" else "price_display", None)]
            messages.append(f"Scraped via {result.source}: {len(result.listing.photos)} photo(s), "
                            f"{len(got)} field(s).")
        listing = listing.merged_with(result.listing)
        listing.source = result.source or listing.source

    if options.manual_path:
        entered = manual_mod.load_manual(options.manual_path)
        listing = listing.merged_with(entered)  # typed values win
        blocked = False  # a hand-filled template is the data; the block is moot
        messages.append(f"Applied manual data from {Path(options.manual_path).name}.")

    if options.overrides is not None:
        listing = listing.merged_with(options.overrides)
        messages.append("Applied row overrides.")

    if listing.missing_required(options.required) and options.interactive:
        listing = listing.merged_with(manual_mod.prompt_interactive(listing))

    return listing, messages, blocked and not listing.address and not listing.photos


def run_one(options: RunOptions, cfg: Config) -> RunResult:
    """Process a single listing. Returns a result rather than raising."""
    log = print if options.verbose else (lambda *a, **k: None)

    listing, messages, stonewalled = acquire(options, cfg)
    result = RunResult(listing=listing, messages=messages)
    for message in messages:
        log(f"  - {message}")

    # Blocked with nothing to show for it: bail out now so the caller can
    # reopen with a visible browser. Presenting a review table of twelve empty
    # fields invites the operator to type the whole listing by hand — or, more
    # often, to quit — when clearing one challenge would have filled it in.
    if stonewalled and not options.solve_challenge:
        result.status = "needs_input"
        result.messages.append("Nothing was scraped — the human check needs clearing.")
        return result

    # Let the operator eyeball and correct the scrape before anything is
    # rendered or uploaded. Skipped when there is no terminal to prompt on,
    # so batch runs and cron jobs are unaffected.
    if options.review and sys.stdin.isatty():
        listing = manual_mod.review_listing(listing, cfg, options.required)
        result.listing = listing

    # --- gate on required fields -----------------------------------------
    missing = listing.missing_required(options.required)
    if missing:
        result.status = "needs_input"
        result.messages.append(f"Missing required field(s): {', '.join(missing)}")
        if options.write_fallback_template:
            workdir = Path(options.workdir).expanduser() / (listing.slug or "unknown-listing")
            template = manual_mod.write_template(workdir / "listing.json", listing)
            result.template_path = template
            log(f"\n  Could not get everything automatically. Missing: {', '.join(missing)}")
            log(f"  A pre-filled template is waiting at:\n    {template}")
            log(f"  Fill in the blanks, then re-run:\n    zillow-reels make --manual {template}")
        else:
            log(f"\n  Could not get the listing automatically (missing: {', '.join(missing)}).")
        return result

    incomplete = listing.missing_optional()
    if incomplete:
        note = f"Rendering without: {', '.join(incomplete)} (these are optional)."
        result.messages.append(note)
        log(f"  - {note}")

    workdir = Path(options.workdir).expanduser() / listing.slug
    workdir.mkdir(parents=True, exist_ok=True)

    # --- photos -----------------------------------------------------------
    log(f"  Downloading photos -> {workdir / 'photos'}")
    # The whole gallery is saved and uploaded; the video uses a prefix of it.
    photos = download_photos(
        listing.photos,
        workdir / "photos",
        max_photos=cfg.max_downloads or None,
        verbose=options.verbose,
    )
    if not photos:
        result.status = "error"
        result.messages.append("No usable photos — nothing to render.")
        log("  ! No usable photos were downloaded.")
        return result
    listing.photos = photos
    result.photo_paths = [Path(p.path) for p in photos if p.path]

    # --- video ------------------------------------------------------------
    if not options.skip_video:
        video_path = workdir / f"{listing.slug}.mp4"
        in_video = photos[: cfg.max_photos] if cfg.max_photos else photos
        if len(in_video) < len(photos):
            log(f"  Using the first {len(in_video)} of {len(photos)} photo(s) in the video")
        log(f"  Building video -> {video_path}")
        result.video_path = build_video(listing, in_video, video_path, cfg, verbose=options.verbose)

    # --- drive ------------------------------------------------------------
    if options.upload:
        from .drive import DriveError, upload_listing  # imported late: optional dep

        log(f"  Uploading to Google Drive folder '{listing.folder_name}'")
        try:
            result.drive = upload_listing(
                listing.folder_name,
                result.video_path,
                result.photo_paths,
                cfg,
                client_secrets=options.client_secrets or None,
                parent_folder_id=options.parent_folder_id or None,
                console=options.console_auth,
                verbose=options.verbose,
            )
        except DriveError as exc:
            # The video exists locally; a Drive problem shouldn't lose the run.
            result.messages.append(f"Drive upload skipped: {exc}")
            log(f"  ! Drive upload failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            result.messages.append(f"Drive upload failed: {exc}")
            log(f"  ! Drive upload failed: {exc}")

    return result
