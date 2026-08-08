# Setup

Start to finish: about 15 minutes, most of it waiting on Google Cloud Console.

- [1. Install](#1-install)
- [2. Google Drive (optional)](#2-google-drive-optional)
- [3. Zillow access](#3-zillow-access)
- [4. Branding](#4-branding)
- [5. Verify](#5-verify)
- [Troubleshooting](#troubleshooting)
- [Where your credentials live](#where-your-credentials-live)

---

## 1. Install

Requires **Python 3.10+**. ffmpeg arrives bundled with `imageio-ffmpeg` — there is nothing
separate to install.

```bash
git clone https://github.com/AbdulHaris633/Zillow-Listing-Social-Media-Video-Generator.git
cd Zillow-Listing-Social-Media-Video-Generator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Check it works:

```bash
.venv/bin/python -m unittest discover tests    # 33 tests, no network needed
```

### Optional extras

```bash
# Real-browser fetching. Strongly recommended — it is the only reliably working
# path past Zillow's bot protection.
.venv/bin/pip install playwright && .venv/bin/playwright install chromium

# Chrome TLS impersonation for the plain-HTTP backend.
.venv/bin/pip install curl_cffi
```

You can render videos with neither of these, using the manual template. See
[the README](README.md#the-manual-template).

---

## 2. Google Drive (optional)

Skip this and everything still works — videos just stay in `out/` locally, and the tool
detects the absence of credentials and skips the upload rather than erroring.

**No credentials ship with this project.** You create your own OAuth client, which takes a
few minutes and is free.

### Create the OAuth client

1. **[Google Cloud Console](https://console.cloud.google.com/)** → create or select a project.
2. **[Enable the Drive API](https://console.cloud.google.com/apis/library/drive.googleapis.com)**
   → *Enable*.
3. **APIs & Services → OAuth consent screen**
   - User type: **External**
   - App name, support email, developer email: anything of yours
4. **Audience** → either
   - **Publish app** ← recommended. The scope used here (`drive.file`) is *not* a sensitive
     scope, so publishing needs no Google verification review and tokens stop expiring; or
   - **Test users → + Add users** → add your own Google account. Works immediately, but
     refresh tokens expire every **7 days** in testing mode.
5. **Credentials → + Create credentials → OAuth client ID**
   - Application type: **Desktop app** ← must be Desktop, *not* Web application
   - **Download JSON**

### Install it

```bash
mkdir -p ~/.config/zillow-reels && chmod 700 ~/.config/zillow-reels
mv ~/Downloads/client_secret_*.json ~/.config/zillow-reels/client_secret.json
chmod 600 ~/.config/zillow-reels/client_secret.json
```

### Authorise

```bash
.venv/bin/python -m zillow_reels auth
```

A browser opens; approve the request. On the "Google hasn't verified this app" screen choose
**Advanced → Go to … (unsafe)** — that warning is generic, and the app is your own, running
locally. You should see:

```
Authorised as you@example.com
Token stored at ~/.config/zillow-reels/token.json
```

### About the scope

The tool requests **`drive.file`** only. That grants access to files it creates itself and
nothing else — it cannot read, modify or even see the rest of your Drive. This is deliberate;
the broader `drive` scope is not requested and is not needed.

To put every listing folder inside one parent folder rather than at your Drive root, copy
that folder's ID out of its URL and set it in `config.toml`:

```toml
drive_parent_folder_id = "1AbCdEfGh..."
```

---

## 3. Zillow access

Zillow runs commercial bot protection. Measured against a live listing, **every unattended
backend was eventually served HTTP 403** — header and TLS hardening improve the odds but do
not settle it. What reliably works is clearing the check yourself once and reusing the cookie.

**First run** — a real browser opens and you press & hold for ~6 seconds:

```bash
./reel "https://www.zillow.com/homedetails/…/12345678_zpid/"
```

The wrapper detects the block, opens the window, waits for you, then carries on to the video
by itself. The cookie is saved to `~/.zillow-profile`.

**Every run after that** is headless and unattended. The challenge typically reappears only
when the cookie lapses, and the same command handles it again.

The press-and-hold gesture is deliberately **not** automated. That check exists to assert a
human is present; scripting it makes a false assertion to the service, and it is also the
first behaviour their detection looks for.

### If you would rather not scrape at all

Two fallbacks need no setup whatsoever:

```bash
./reel <url> --from-html saved-page.html   # Cmd-S the listing in your own browser
./reel --manual listing.json               # type the details in
```

### For production use

The durable answer is licensed data, not a cookie: Zillow's **Bridge Interactive** API, or an
**IDX/RETS feed** from the listing brokerage's MLS. A listing agent can generally get feed
access for their own listings. That removes the challenge, the terms-of-service exposure, and
the photo-licensing question together — MLS images are typically licensed to the listing
brokerage, not to whoever downloads them.

---

## 4. Branding

```bash
cp config.example.toml config.toml
```

`config.toml` is gitignored. The settings worth changing first:

| Key | Default | |
|---|---|---|
| `accent` | `#E8B44A` | Pills, dividers, call to action |
| `logo_path` | — | PNG with transparency, centred in the card footer |
| `cta_text` | `DM for a private showing` | Outro call to action |
| `disclaimer` | — | Licence line or small print on the outro |
| `max_photos` | `14` | Photos **used in the video** |
| `max_downloads` | `0` | Photos **saved and uploaded**; 0 = the whole gallery |
| `music_path` | — | Looped and faded to length |

Fonts auto-detect (Arial / Helvetica / DejaVu). Set `font_bold` and `font_regular` for a
brand typeface.

---

## 5. Verify

```bash
./reel "https://www.zillow.com/homedetails/…/12345678_zpid/"
```

Expected: a review table you accept with Enter, then

```
out/<address>/
├── <address>.mp4          1080x1920, H.264, ~2.15s per photo
└── photos/                the full gallery
```

and, if Drive is connected, the same content under a folder named after the property.

---

## Troubleshooting

### `Error 403: access_denied` during authorisation

> App is currently being tested and can only be accessed by developer-approved testers.

Your Google account is not on the allow-list. Fix with step 2.4 — publish the app, or add
yourself under *Test users*.

### `Access blocked: this app does not comply with Google's policies`

The OAuth client was created as a **Web application**. Delete it and create a **Desktop app**
client instead. A correct one contains an `"installed"` key and an `http://localhost`
redirect URI.

### `Drive upload failed: timed out`

Usually a broken IPv6 path to `www.googleapis.com`: the TCP connect stalls ~30 s before
falling back to IPv4, while other Google hosts answer instantly. The client is configured
with a 300 s timeout and retries to ride this out, so it should succeed after a long first
pause. To confirm it is your network rather than the tool:

```bash
.venv/bin/python -c "
import socket, time
for h in ['www.googleapis.com', 'oauth2.googleapis.com']:
    t = time.time(); socket.create_connection((h, 443), timeout=40).close()
    print(f'{h}: {time.time()-t:.1f}s')"
```

A multi-second result for the first host and instant for the second confirms it. Disabling
IPv6 on the interface, or fixing the router's IPv6 configuration, is the real remedy.

### Uploads stop after 7 days

Refresh tokens expire in testing mode. Publish the app (step 2.4), or re-run:

```bash
rm ~/.config/zillow-reels/token.json && .venv/bin/python -m zillow_reels auth
```

### A pre-filled `listing.json` appears instead of a video

The scrape could not get the required fields **and** the browser retry did not complete —
usually the challenge window was closed or timed out. Just run again. If Chrome never opened,
you invoked `zillow-reels` directly; the auto-retry lives in the `./reel` wrapper.

### `DRIVE[@]: unbound variable`

An old copy of `reel` on bash 3.2 (the version macOS ships). Fixed in current versions; pull
the latest.

### Video renders but text is tiny or missing

No system TrueType font was found, so Pillow fell back to its bitmap default. Set
`font_bold` and `font_regular` in `config.toml` to any `.ttf` on your machine.

### `zillow-reels: command not found`

Use `./reel` or `.venv/bin/python -m zillow_reels`. Installing with
`.venv/bin/pip install -e .` also puts a `zillow-reels` entry point inside the venv.

---

## Where your credentials live

None of these are in the repository, and all are gitignored by name.

| What | Path | Notes |
|---|---|---|
| Google OAuth client | `~/.config/zillow-reels/client_secret.json` | mode 600. Identifies the app; harmless alone |
| Google access token | `~/.config/zillow-reels/token.json` | mode 600. **The sensitive one** — uploads as you, limited to `drive.file` |
| Zillow session | `~/.zillow-profile/` | A Chrome profile, ~100 MB. Delete freely; you redo the challenge once |

Revoke Drive access any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

Moving machines: copy `~/.config/zillow-reels/` and nothing else. Do **not** put it in
Dropbox, iCloud, or a repository.
