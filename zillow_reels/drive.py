"""Google Drive upload over OAuth 2.0 — no service accounts, no baked-in tokens.

The user authorises once in a browser; the refresh token is written to
~/.config/zillow-reels/token.json with 0600 permissions and reused after that.
The requested scope is `drive.file`, which only grants access to files this
tool itself creates — it cannot read anything else in the user's Drive.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from .config import APP_DIR, Config

# Least privilege: per-file access to what we create, nothing more.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER_MIME = "application/vnd.google-apps.folder"

# Generous on purpose. On networks with a broken IPv6 path to
# www.googleapis.com the TCP connect stalls for ~30s before falling back to
# IPv4 — well past the client library's default, which surfaces as an
# unhelpful bare "timed out" long before a single byte has been sent.
HTTP_TIMEOUT = 300

# Transient 5xx/429s from Drive are common on large batches; the client library
# retries with exponential backoff when asked to.
RETRIES = 5


class DriveError(RuntimeError):
    pass


def _build_service(creds):
    """Drive client wired to an HTTP transport with a workable timeout.

    `build(credentials=…)` constructs its own transport with the library
    default, so the timeout has to be injected by supplying the http object
    outright — the two arguments are mutually exclusive.
    """
    import google_auth_httplib2
    import httplib2
    from googleapiclient.discovery import build

    authorized = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=HTTP_TIMEOUT))
    return build("drive", "v3", http=authorized, cache_discovery=False)


def _imports():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        return Request, Credentials, InstalledAppFlow, build, MediaFileUpload
    except ImportError as exc:  # pragma: no cover
        raise DriveError(
            "Google API libraries are missing. Run: pip install -r requirements.txt"
        ) from exc


def _find_client_secrets(explicit: str | Path | None) -> Path:
    """Locate the OAuth client secrets JSON downloaded from Cloud Console."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("ZILLOW_REELS_CLIENT_SECRETS"):
        candidates.append(Path(os.environ["ZILLOW_REELS_CLIENT_SECRETS"]).expanduser())
    candidates += [
        APP_DIR / "client_secret.json",
        Path("client_secret.json"),
        Path("credentials.json"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise DriveError(
        "No Google OAuth client secrets found.\n"
        "  1. Google Cloud Console -> APIs & Services -> Credentials\n"
        "  2. Create an OAuth client ID of type 'Desktop app', enable the Drive API\n"
        f"  3. Download the JSON and save it to {APP_DIR / 'client_secret.json'}\n"
        "     (or pass --client-secrets /path/to/file.json)"
    )


class DriveClient:
    def __init__(self, service):
        self.service = service

    # --- auth -------------------------------------------------------------

    @classmethod
    def authorize(
        cls,
        client_secrets: str | Path | None = None,
        token_path: str | Path | None = None,
        *,
        console: bool = False,
    ) -> "DriveClient":
        Request, Credentials, InstalledAppFlow, build, _ = _imports()

        token_file = Path(token_path).expanduser() if token_path else APP_DIR / "token.json"
        creds = None
        if token_file.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            except ValueError:
                creds = None  # corrupt or wrong-scope token: re-authorise

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:  # noqa: BLE001 - revoked/expired refresh token
                creds = None

        if not creds or not creds.valid:
            secrets = _find_client_secrets(client_secrets)
            flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
            if console:
                creds = flow.run_console()  # headless/SSH
            else:
                creds = flow.run_local_server(port=0, prompt="consent")

            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(creds.to_json(), encoding="utf-8")
            os.chmod(token_file, 0o600)  # refresh token — keep it private

        return cls(_build_service(creds))

    @classmethod
    def from_config(cls, cfg: Config, client_secrets: str | Path | None = None, console: bool = False):
        return cls.authorize(client_secrets or cfg.drive_client_secrets or None, cfg.token_path, console=console)

    # --- operations -------------------------------------------------------

    def ensure_folder(self, name: str, parent_id: str | None = None) -> str:
        """Return the id of a folder with this name, creating it if needed.

        Re-running the tool on the same listing reuses the folder instead of
        creating '123 Main St' four times.
        """
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = [f"name = '{escaped}'", f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
        if parent_id:
            query.append(f"'{parent_id}' in parents")

        response = (
            self.service.files()
            .list(q=" and ".join(query), spaces="drive", fields="files(id, name)", pageSize=5)
            .execute(num_retries=RETRIES)
        )
        files = response.get("files", [])
        if files:
            return files[0]["id"]

        metadata = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]
        created = self.service.files().create(body=metadata, fields="id").execute(num_retries=RETRIES)
        return created["id"]

    def upload_file(self, path: str | Path, folder_id: str, *, replace: bool = True) -> dict:
        """Upload one file, replacing a same-named file in the folder."""
        _, _, _, _, MediaFileUpload = _imports()
        path = Path(path).expanduser()
        if not path.exists():
            raise DriveError(f"file not found: {path}")

        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # Resumable matters for the MP4; harmless for photos.
        media = MediaFileUpload(str(path), mimetype=mimetype, resumable=path.stat().st_size > 2_000_000)

        if replace:
            escaped = path.name.replace("\\", "\\\\").replace("'", "\\'")
            existing = (
                self.service.files()
                .list(
                    q=f"name = '{escaped}' and '{folder_id}' in parents and trashed = false",
                    spaces="drive",
                    fields="files(id)",
                    pageSize=1,
                )
                .execute(num_retries=RETRIES)
                .get("files", [])
            )
            if existing:
                return (
                    self.service.files()
                    .update(fileId=existing[0]["id"], media_body=media, fields="id, name, webViewLink")
                    .execute(num_retries=RETRIES)
                )

        return (
            self.service.files()
            .create(
                body={"name": path.name, "parents": [folder_id]},
                media_body=media,
                fields="id, name, webViewLink",
            )
            .execute(num_retries=RETRIES)
        )

    def folder_link(self, folder_id: str) -> str:
        return f"https://drive.google.com/drive/folders/{folder_id}"


def upload_listing(
    listing_folder_name: str,
    video_path: str | Path | None,
    photo_paths: list[Path],
    cfg: Config,
    *,
    client_secrets: str | Path | None = None,
    parent_folder_id: str | None = None,
    console: bool = False,
    verbose: bool = True,
) -> dict:
    """Create '<address>/' with the MP4 and a 'Photos/' subfolder inside it."""
    client = DriveClient.from_config(cfg, client_secrets, console=console)
    parent = parent_folder_id or cfg.drive_parent_folder_id or None

    folder_id = client.ensure_folder(listing_folder_name, parent)
    result: dict = {"folder_id": folder_id, "folder_url": client.folder_link(folder_id), "photos": 0}

    if video_path:
        if verbose:
            print(f"    uploading {Path(video_path).name}…")
        uploaded = client.upload_file(video_path, folder_id)
        result["video_url"] = uploaded.get("webViewLink", "")

    if photo_paths:
        photos_id = client.ensure_folder("Photos", folder_id)
        result["photos_folder_url"] = client.folder_link(photos_id)
        for index, photo in enumerate(photo_paths, 1):
            if verbose:
                print(f"    uploading photo {index}/{len(photo_paths)}…", end="\r")
            try:
                client.upload_file(photo, photos_id)
                result["photos"] += 1
            except Exception as exc:  # noqa: BLE001 - keep going, report at the end
                print(f"\n    ! photo upload failed ({Path(photo).name}): {exc}")
        if verbose:
            print(f"    uploaded {result['photos']} photo(s) to Photos/    ")

    return result
