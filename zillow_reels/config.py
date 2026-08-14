"""Branding, timing and path configuration, loaded from an optional TOML file.

Every value has a working default, so the tool runs with no config file at all.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore[assignment]


APP_DIR = Path(os.environ.get("ZILLOW_REELS_HOME", Path.home() / ".config" / "zillow-reels"))

# Ordered by preference; the first that exists is used for text rendering.
FONT_CANDIDATES_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttc",
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_CANDIDATES_REGULAR = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


@dataclass
class Config:
    # Canvas — 1080x1920 is the Reels/TikTok/Shorts standard.
    width: int = 1080
    height: int = 1920
    fps: int = 30

    # Timing, in seconds.
    title_seconds: float = 3.0
    photo_seconds: float = 2.6
    outro_seconds: float = 4.5
    # Per page of the rental availability table. Long enough to read eight
    # rows; set to 0 to leave the units out of the video entirely.
    units_seconds: float = 5.0
    crossfade_seconds: float = 0.45
    max_photos: int = 14        # photos used in the video
    max_downloads: int = 0      # photos saved to disk / Drive; 0 = the whole gallery
    # Which photo the "JUST SOLD" card is built on, 1-based. 0 means the first,
    # which is Zillow's own lead photo and usually the right one.
    card_photo: int = 0

    # Ken Burns pan/zoom. Off by default: listing photos are dense with
    # high-frequency detail (siding, brick, shingles, grass) that crawls when
    # panned, and a 3:2 photo cropped to 9:16 has to be enlarged to fill the
    # frame, which magnifies the crawl. A still photo cannot shimmer at all.
    # Raise it (1.04 is gentle, 1.12 strong) if you want the movement back.
    zoom: float = 1.0

    # Brand.
    accent: str = "#E8B44A"
    background: str = "#12151C"
    text: str = "#FFFFFF"
    muted: str = "#B9C0CC"
    logo_path: str = ""
    cta_text: str = "DM for a private showing"
    disclaimer: str = ""

    # Typography — leave blank to auto-detect a system font.
    font_bold: str = ""
    font_regular: str = ""

    # Audio.
    music_path: str = ""
    music_volume: float = 0.14
    music_fade_seconds: float = 1.5

    # Per-photo captions: "auto" uses scraped/manual captions, "off" hides them.
    captions: str = "auto"

    # Encoding.
    video_codec: str = "libx264"
    video_preset: str = "medium"
    video_bitrate: str = "8M"

    # Google Drive.
    drive_client_secrets: str = ""
    drive_parent_folder_id: str = ""
    drive_token_path: str = ""

    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    @property
    def token_path(self) -> Path:
        return Path(self.drive_token_path).expanduser() if self.drive_token_path else APP_DIR / "token.json"

    def resolved_font(self, bold: bool = False) -> str | None:
        explicit = self.font_bold if bold else self.font_regular
        if explicit and Path(explicit).expanduser().exists():
            return str(Path(explicit).expanduser())
        for candidate in FONT_CANDIDATES_BOLD if bold else FONT_CANDIDATES_REGULAR:
            if Path(candidate).exists():
                return candidate
        return None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        """Read config.toml, falling back to defaults when absent."""
        candidates = [Path(path)] if path else [Path("config.toml"), APP_DIR / "config.toml"]
        for candidate in candidates:
            candidate = candidate.expanduser()
            if not candidate.exists():
                continue
            if tomllib is None:
                raise RuntimeError(
                    f"{candidate} found but TOML support is missing. "
                    "Install tomli (pip install tomli) or use Python 3.11+."
                )
            with candidate.open("rb") as handle:
                data = tomllib.load(handle)
            # Accept both a flat file and a [branding]/[video]/[drive] layout.
            flat: dict[str, Any] = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    flat.update(value)
                else:
                    flat[key] = value
            known = {f.name for f in fields(cls)}
            config = cls(**{k: v for k, v in flat.items() if k in known})
            config.extras = {k: v for k, v in flat.items() if k not in known}
            return config
        return cls()
