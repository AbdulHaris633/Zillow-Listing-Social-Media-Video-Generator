# Zillow Reels

Turns a Zillow listing into a 1080×1920 MP4 — title card, photo slideshow, agent outro —
and files it in Google Drive in a folder named after the property, alongside a `Photos/`
subfolder with the images.

The output is a finished vertical video ready to download and post to Reels / TikTok /
Shorts by hand. It does not auto-post anywhere and does not export an editable project file.

```
out/142-maple-ridge-ct-fairview-mo-65010/
├── 142-maple-ridge-ct-fairview-mo-65010.mp4
└── photos/
    ├── 01-kitchen.jpg
    └── …

Google Drive/
└── 142 Maple Ridge Ct, Fairview, MO 65010/
    ├── 142-maple-ridge-ct-fairview-mo-65010.mp4
    └── Photos/
```

---

## About Zillow's bot protection — read this first

Zillow runs commercial bot protection (PerimeterX/HUMAN) and publishes no listing API.

**What was actually measured** against a live listing during development, not guessed:

| Backend | Hardening applied | Result |
|---|---|---|
| `requests` | Full Chrome header set, homepage warm-up for cookies, randomised pacing, retry with backoff | one HTTP 200, then `403` on every subsequent run |
| `curl` (curl_cffi) | Chrome TLS/JA3 impersonation | `403` |
| `browser` headless | Playwright, stealth init script, human-ish scrolling | `403` |
| `browser` + real Chrome, visible | `channel=chrome`, persistent profile | `403`, then the press-and-hold challenge |
| **`--solve-challenge`** | **you clear the challenge once, by hand** | **HTTP 200, full data** |
| **saved profile, headless, unattended** | **reuses that cookie** | **HTTP 200, no challenge** |

So: **hardening alone does not settle it, and anyone claiming otherwise is guessing.**
What works is clearing the check once yourself and reusing the cookie.

```bash
# One time — opens a real browser, you press & hold, cookie is saved
zillow-reels make "<url>" --fetch browser --browser-channel chrome \
    --show-browser --solve-challenge --browser-profile ~/.zillow-profile

# Every run after that — headless, unattended, no challenge
zillow-reels make "<url>" --fetch browser --browser-channel chrome \
    --browser-profile ~/.zillow-profile
```

The press-and-hold gesture is deliberately **not** automated. That check exists to assert a
human is present; scripting it makes a false assertion to the service, and it is also the
first behaviour their detection looks for. Expect to redo it occasionally when the cookie
expires. `--proxy` is available if you route through your own egress.

### If the cookie lapses mid-batch

Two fallbacks, neither of which needs any setup:

| Rung | Command | Works when |
|---|---|---|
| Saved page | `make <url> --from-html page.html` | Always — `Cmd-S` in your own browser |
| Manual entry | `make --manual listing.json` | Always — you type the fields |

The saved-page rung is fully supported by a dedicated DOM parser: everything parses out of a
browser-saved file exactly as if the scrape had worked. When anything comes back short, the
tool writes a **pre-filled** template with what it did get and a `_missing` list, so you fill
three fields instead of twelve:

```
Could not get everything automatically. Missing: address, price, photos
A pre-filled template is waiting at:
  out/listing/listing.json
Fill in the blanks, then re-run:
  zillow-reels make --manual out/listing/listing.json
```

### The durable fix

If this is going into regular production use, the sustainable answer is licensed data, not
a cookie: Zillow's **Bridge Interactive** API, or an **IDX/RETS feed** from the listing
brokerage's MLS. A listing agent can generally get MLS feed access for their own listings.
That removes the challenge, the ToS exposure, and the photo-licensing question in one step —
MLS images are typically licensed to the listing brokerage, not to whoever downloads them.

---

## Setup

**Full instructions for macOS, Windows and Linux — including Google Drive OAuth and
troubleshooting — are in [SETUP.md](SETUP.md).**

Requires Python 3.10+. ffmpeg comes bundled via `imageio-ffmpeg`; no separate install.

```bash
git clone <repo> && cd zillow-reels
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt      # macOS / Linux
py -m venv .venv && .venv\Scripts\pip install -r requirements.txt       # Windows
```

Optional extras:

```bash
pip install playwright && playwright install chromium   # --fetch browser
cp config.example.toml config.toml                      # branding, timing, music
```

### Google Drive (OAuth)

No credentials are bundled or hardcoded. You supply your own OAuth client, authorise once
in a browser, and the refresh token is cached locally at mode `600`.

1. [Google Cloud Console](https://console.cloud.google.com/) → create or pick a project.
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → Credentials** → *Create credentials* → **OAuth client ID** →
   application type **Desktop app** → download the JSON.
4. Save it as `~/.config/zillow-reels/client_secret.json` (or pass `--client-secrets`).
5. Authorise:

```bash
zillow-reels auth
# Authorised as you@example.com
# Token stored at ~/.config/zillow-reels/token.json
```

The requested scope is `drive.file` — **the tool can only see and touch files it creates
itself.** It cannot read anything else in your Drive.

While the OAuth consent screen is in "Testing", add yourself under *Audience → Test users*.
Re-authorise with `rm ~/.config/zillow-reels/token.json && zillow-reels auth`.

---

## Usage

**The whole process in one command** — paste a link, get a video:

```bash
./reel "https://www.zillow.com/homedetails/…/12345678_zpid/"
./reel                              # or run it bare and it asks for the link
```

It scrapes, **shows you what it found and waits**, then downloads the photos, renders the
MP4 and uploads to Drive:

```
──────────────────────────────────────────────────────────────────────────
  Scraped listing — check it over before the video is built
──────────────────────────────────────────────────────────────────────────
    1  Address       142 Maple Ridge Ct, Fairview, MO 65010
    2  Price         $450,000
    3  Beds          4
 !  6  Status        (missing)
   12  Description   Rare direct lake frontage meets beautifully updated liv…
     p  Photos        60 (first 14 will be used) · 8 captioned
──────────────────────────────────────────────────────────────────────────
  Enter = build the video · number = edit · p = photos · q = quit:
```

Press Enter and it builds. Type a number to correct that one field — typed values go
through the same parsing the scraper's output does, so `$525,000` and `525000` both work,
and retyping the address re-splits the city, state and ZIP. `p` opens a photo submenu:

| | |
|---|---|
| `s` | build a numbered contact sheet of every photo and open it — pick by eye, not by filename |
| `n 8` | how many photos go in the video |
| `k 1,6,3,11` | pick exactly those, **in that order**; sets the count to match |
| `d 28,33-34` | delete photos entirely (not saved or uploaded either) |

`k` and `d` differ in an important way: `k` only chooses what goes *in the video* — everything
else is still saved and uploaded. `d` removes photos from the run completely. Zillow galleries
routinely end with floor plans, upgrade lists and plat maps, which the contact sheet makes
obvious at a glance. Anything still
missing is flagged with `!`, and Enter is refused until address, price and one photo exist.

The review step is skipped automatically when there's no terminal attached, so batch runs
and scheduled jobs are unaffected. Add `--review` to the bare CLI to get it there too. If Zillow asks for
the human check it opens a browser, waits for you to press & hold, and continues by itself —
no second command. The cookie is saved to `~/.zillow-profile`, so it usually only asks once.
Drive upload is skipped automatically until OAuth credentials exist, so a first run produces
a video rather than a wall of setup instructions.

Other modes and pass-through flags:

```bash
./reel --manual listing.json        # no scraping at all
./reel batch listings.csv           # a spreadsheet of listings
./reel <url> --max-photos 8 --no-drive --music bed.mp3
```

Override the profile location with `ZILLOW_PROFILE=/path ./reel …`.

Or drive the CLI directly:

```bash
# Automated, once a profile cookie exists (see the section above)
zillow-reels make "https://www.zillow.com/homedetails/…/12345678_zpid/" \
    --fetch browser --browser-channel chrome --browser-profile ~/.zillow-profile

# Try the live fetch bare; falls back to a pre-filled template if blocked
zillow-reels make "https://www.zillow.com/homedetails/…/12345678_zpid/"

# No-setup path: save the page in your browser first
zillow-reels make "https://www.zillow.com/homedetails/…" --from-html ~/Downloads/listing.html

# Fully manual
zillow-reels template listing.json     # write a blank template
zillow-reels make --manual listing.json

# Prompt for whatever is still missing instead of writing a template
zillow-reels make "<url>" --interactive

# See what a listing yields without rendering anything
zillow-reels probe "<url>" --from-html page.html

# Render locally, skip the upload
zillow-reels make --manual listing.json --no-drive
```

### Batch

One row per listing. A `url` column, a `manual` column, or both:

```csv
url,manual
https://www.zillow.com/homedetails/…/12345678_zpid/,
,listings/142-maple-ridge.json
```

```bash
zillow-reels batch listings.csv
```

Any other column (`address`, `price`, `beds`, `photo_folder`, …) is treated as an override
for that row, which is a quick way to patch the one field a scrape keeps missing — or to
run entirely from a spreadsheet plus a folder of photos per property. A row that fails or
needs input never stops the batch — every row is attempted and the outcomes land in
`out/batch-report.csv`.

### The manual template

```json
{
  "address": "142 Maple Ridge Ct, Fairview, MO 65010",
  "price": "389900",
  "beds": "4",
  "baths": "3",
  "sqft": "2480",
  "description": "Beautifully maintained home on a quiet cul-de-sac…",
  "agent_name": "Jane Realtor",
  "agent_phone": "(573) 555-0142",
  "brokerage": "Columbia Realty Group",
  "photo_folder": "./photos",
  "photos": [
    { "url": "https://…/kitchen.jpg", "caption": "Kitchen" },
    { "path": "photos/back-yard.jpg", "caption": "Backyard" }
  ]
}
```

- Only `address`, `price` and at least one photo are required. Everything else is optional
  and simply omitted from the video — a listing with no `beds` renders without a beds pill,
  not with a blank one.
- `photo_folder` expands to every image in that folder, sorted by filename.
- `photos` accepts bare URLs, local paths, or `{url|path, caption}` objects. Relative paths
  resolve against the template's own location, so a template and its photo folder travel
  together.
- `caption` drives the per-photo caption bar ("Kitchen", "Primary Bedroom").

Scraped captions are best-effort: Zillow only groups photos by room on *some* listings
(generally those with a 3D tour). Where that section exists the captioned room shots lead
the slideshow and the rest of the gallery follows; where it doesn't, the video renders
without caption bars. Of the listings tested, some had room grouping and some had none.

---

## Configuration

`config.toml` (see `config.example.toml`). Common overrides also exist as CLI flags:
`--max-photos`, `--photo-seconds`, `--music`, `--logo`, `--captions off`.

| Key | Default | Notes |
|---|---|---|
| `accent` | `#E8B44A` | Pills, dividers, CTA |
| `logo_path` | — | PNG with transparency, centred in the card footer |
| `cta_text` | `DM for a private showing` | Outro call to action |
| `disclaimer` | — | Small print on the outro (licence line, etc.) |
| `photo_seconds` | `2.6` | Per photo |
| `max_photos` | `14` | ≈45 s total at defaults |
| `zoom` | `1.12` | Ken Burns pan/zoom; `1.0` disables it |
| `captions` | `auto` | `off` hides the caption bar |
| `music_path` | — | Looped and faded to length at `music_volume` (0.14) |
| `drive_parent_folder_id` | — | Create listing folders inside an existing Drive folder |

Fonts auto-detect from the system (Arial / Helvetica / DejaVu); set `font_bold` and
`font_regular` to use a brand typeface.

---

## How it works

```
scrape.py    fetch (curl_cffi | requests | playwright | saved file) → detect blocks → parse
manual.py    blank / pre-filled templates, interactive prompt
photos.py    parallel download, decode-verify, de-duplicate, cap
cards.py     title + outro cards, caption bars (Pillow)
video.py     one timeline, one frame function → MP4
drive.py     OAuth, folder-per-listing, resumable upload
pipeline.py  acquire → photos → video → Drive
```

Two implementation notes that matter if you extend this:

**Parsing is shape-agnostic.** Zillow reshuffles its Next.js payload several times a year,
so rather than hardcoding a key path, the parser walks the whole JSON tree — including
JSON-encoded strings nested inside it, which is where `gdpClientCache` lives — and scores
dicts by how many property-ish keys they carry. It also reads schema.org JSON-LD and
OpenGraph tags, merging richest-first. That survives most redesigns without a code change.

**The video is rendered without moviepy's effects API.** All text is drawn with Pillow
(moviepy's `TextClip` needs ImageMagick on 1.x and changed signature in 2.x — the most
common install failure in projects like this), and the pan/zoom and crossfades are computed
in numpy against a single timeline. moviepy is left responsible only for encoding and audio,
behind a small compat shim, so the same code runs on moviepy 1.x and 2.x.

Output is H.264 High / `yuv420p` with `+faststart`, which is what every platform's
transcoder expects.

---

## Tests

```bash
python -m unittest discover tests
```

22 tests, no network required. Covers payload parsing (against Zillow's real nesting shape),
the DOM parser (against a captured real page), JSON-LD fallback, block detection, header
self-consistency, request pacing, the manual round-trip, and a genuine end-to-end render
that produces a playable MP4. Several are regressions for bugs found during the build: a
Ken Burns crop box that could exceed its source by a fraction of a pixel, a moviepy audio
reader that mis-recurses on any request over ~25k samples, and over-fetched photos being
left in the folder that gets uploaded to Drive.

---

## Not included

- **Web UI.** CLI and CSV batch only. A small Flask/FastAPI form over `pipeline.run_one`
  would be the natural next step — the pipeline is already a single function call that
  takes a `RunOptions` and returns a `RunResult`.
- **Auto-posting** to Instagram/TikTok/YouTube, per the spec.
- **Automated solving of the press-and-hold challenge.** The tool waits for you to clear it
  and then reuses the cookie; it does not script the gesture. See the top of this file.
