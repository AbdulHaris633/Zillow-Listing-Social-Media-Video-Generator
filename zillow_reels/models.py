"""The Listing record every part of the pipeline speaks.

Scraping, manual entry and the CSV batch loader all produce one of these; the
video renderer and Drive uploader only ever consume one. Every field except the
address is optional, because partial data is the normal case with Zillow — the
renderer skips what it doesn't have rather than failing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


def _clean(value: Any) -> str:
    """Collapse whitespace and drop HTML entities left behind by scraping."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _as_number(value: Any) -> float | None:
    """Pull a number out of whatever Zillow (or a human) put in the field."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def format_count(value: float | None) -> str:
    """3.0 -> '3', 2.5 -> '2.5'. Baths are the only field that needs the half."""
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


@dataclass
class Photo:
    """One listing image. `url` is remote, `path` is set once downloaded."""

    url: str = ""
    caption: str = ""
    path: Path | None = None
    width: int = 0

    @property
    def source(self) -> str:
        return str(self.path) if self.path else self.url

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.url}
        if self.caption:
            out["caption"] = self.caption
        if self.path:
            out["path"] = str(self.path)
        return out

    @classmethod
    def from_any(cls, raw: Any) -> "Photo | None":
        """Accept a bare URL/path string or a {url, caption} mapping."""
        if isinstance(raw, str):
            raw = raw.strip()
            if not raw:
                return None
            if raw.startswith(("http://", "https://")):
                return cls(url=raw)
            return cls(path=Path(raw).expanduser())
        if isinstance(raw, dict):
            url = _clean(raw.get("url") or raw.get("src") or "")
            path = raw.get("path") or raw.get("file")
            if not url and not path:
                return None
            return cls(
                url=url,
                caption=_clean(raw.get("caption") or raw.get("title") or ""),
                path=Path(str(path)).expanduser() if path else None,
                width=int(_as_number(raw.get("width")) or 0),
            )
        return None


@dataclass
class Listing:
    """Everything the video and the Drive folder are built from."""

    url: str = ""
    street: str = ""
    city: str = ""
    state: str = ""
    zipcode: str = ""
    price: float | None = None
    price_text: str = ""
    beds: float | None = None
    baths: float | None = None
    sqft: float | None = None
    # Display overrides for the three stats above, used where one number is a
    # lie: an apartment building spans "1-2" beds and "513-717" sq ft. The
    # numeric fields keep the low end so sorting and gating still work.
    beds_text: str = ""
    baths_text: str = ""
    sqft_text: str = ""
    lot_size: str = ""
    year_built: str = ""
    home_type: str = ""
    status: str = ""
    description: str = ""
    agent_name: str = ""
    agent_phone: str = ""
    brokerage: str = ""
    photos: list[Photo] = field(default_factory=list)

    # Provenance, for the run report — not rendered into the video.
    source: str = ""
    notes: list[str] = field(default_factory=list)

    # --- derived text -----------------------------------------------------

    @property
    def locality(self) -> str:
        """'Columbia, MO 65203' — the second line of a US postal address."""
        parts = ", ".join(p for p in (self.city, self.state) if p)
        return f"{parts} {self.zipcode}".strip() if self.zipcode else parts

    @property
    def address(self) -> str:
        """Single-line address, as complete as the data allows."""
        return ", ".join(p for p in (self.street, self.locality) if p)

    @property
    def address_line1(self) -> str:
        return self.street or self.address

    @property
    def address_line2(self) -> str:
        return self.locality if self.street else ""

    @property
    def price_display(self) -> str:
        # An explicit price_text wins: it is only ever set deliberately (a
        # manual template, or a rental where the number alone would read as a
        # sale price — "$1,014" instead of "$1,014+/mo").
        if self.price_text:
            return self.price_text
        if self.price:
            return f"${int(round(self.price)):,}"
        return ""

    @property
    def eyebrow(self) -> str:
        """The banner line on the title card, driven by listing status."""
        status = (self.status or "").upper().replace("-", "_")
        if "SOLD" in status:
            return "JUST SOLD"
        if "PENDING" in status or "CONTINGENT" in status:
            return "SALE PENDING"
        if "RENT" in status:
            return "FOR RENT"
        if "COMING_SOON" in status:
            return "COMING SOON"
        return "JUST LISTED"

    def stats(self) -> list[tuple[str, str]]:
        """(value, label) pills for the title card. Missing stats drop out."""
        out: list[tuple[str, str]] = []

        def pill(text: str, number: float | None, singular: str, plural: str) -> None:
            if not (text or number):
                return
            shown = text or format_count(number)
            # A range is always plural ("1-2 BEDS"), but a text override that
            # collapsed to one value is not — every unit having one bath should
            # still read "1 BATH".
            many = "-" in shown if text else number != 1
            out.append((shown, plural if many else singular))

        pill(self.beds_text, self.beds, "BED", "BEDS")
        pill(self.baths_text, self.baths, "BATH", "BATHS")
        if self.sqft_text or self.sqft:
            out.append((self.sqft_text or f"{int(self.sqft):,}", "SQ FT"))
        return out

    @property
    def folder_name(self) -> str:
        """Drive folder name. Address first so folders sort geographically."""
        name = self.address or self.street or "Untitled Listing"
        name = re.sub(r'[\\/:*?"<>|]+', "-", name)
        return re.sub(r"\s+", " ", name).strip(" .-")[:120] or "Untitled Listing"

    @property
    def slug(self) -> str:
        """Filesystem-safe stem for local working files and the MP4."""
        base = re.sub(r"[^a-zA-Z0-9]+", "-", self.address or "listing").strip("-").lower()
        return (base or "listing")[:80]

    # --- validation -------------------------------------------------------

    REQUIRED = ("address", "price", "photos")

    def missing_required(self) -> list[str]:
        """What must be filled in before a video is worth rendering."""
        missing = []
        if not self.address:
            missing.append("address")
        if not self.price_display:
            missing.append("price")
        if not self.photos:
            missing.append("photos")
        return missing

    def missing_optional(self) -> list[str]:
        """Fields that degrade the video but don't block it."""
        checks = {
            "beds": self.beds,
            "baths": self.baths,
            "sqft": self.sqft,
            "description": self.description,
            "agent_name": self.agent_name,
        }
        return [name for name, value in checks.items() if not value]

    # --- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source", None)
        data.pop("notes", None)
        data["photos"] = [p.to_dict() for p in self.photos]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Listing":
        """Build from a manual template, CSV row or parsed page.

        Tolerant by design: unknown keys are ignored, a few common aliases are
        accepted, and a full 'address' string is split when the parts are absent.
        """
        get = lambda *keys: next(  # noqa: E731
            (data[k] for k in keys if k in data and data[k] not in (None, "")), None
        )

        listing = cls(
            url=_clean(get("url", "zillow_url", "link")),
            street=_clean(get("street", "streetAddress", "address_line1")),
            city=_clean(get("city", "addressLocality")),
            state=_clean(get("state", "addressRegion")),
            zipcode=_clean(get("zipcode", "zip", "postalCode")),
            price=_as_number(get("price", "listPrice")),
            price_text=_clean(get("price_text")),
            beds=_as_number(get("beds", "bedrooms")),
            baths=_as_number(get("baths", "bathrooms")),
            sqft=_as_number(get("sqft", "livingArea", "square_feet", "livingAreaValue")),
            beds_text=_clean(get("beds_text")),
            baths_text=_clean(get("baths_text")),
            sqft_text=_clean(get("sqft_text")),
            lot_size=_clean(get("lot_size", "lotSize")),
            year_built=_clean(get("year_built", "yearBuilt")),
            home_type=_clean(get("home_type", "homeType", "propertyType")),
            status=_clean(get("status", "homeStatus")),
            description=_clean(get("description", "summary")),
            agent_name=_clean(get("agent_name", "agentName", "listing_agent")),
            agent_phone=_clean(get("agent_phone", "agentPhoneNumber", "phone")),
            brokerage=_clean(get("brokerage", "brokerName", "broker")),
        )

        raw_photos = get("photos", "images", "photo_urls")
        if isinstance(raw_photos, str):
            raw_photos = [p for p in re.split(r"[\n,]+", raw_photos) if p.strip()]
        if isinstance(raw_photos, Iterable) and not isinstance(raw_photos, (str, bytes)):
            listing.photos = [p for p in (Photo.from_any(r) for r in raw_photos) if p]

        # A single 'address' string with no parts: split off "City, ST 12345".
        full = _clean(get("address"))
        if full and not listing.street:
            match = re.match(
                r"^(?P<street>.+?),\s*(?P<city>[^,]+),\s*(?P<state>[A-Za-z]{2})\.?\s*"
                r"(?P<zip>\d{5}(?:-\d{4})?)?\s*$",
                full,
            )
            if match:
                listing.street = match.group("street").strip()
                listing.city = listing.city or match.group("city").strip()
                listing.state = listing.state or match.group("state").upper()
                listing.zipcode = listing.zipcode or (match.group("zip") or "")
            else:
                listing.street = full

        return listing

    def merged_with(self, other: "Listing") -> "Listing":
        """Overlay `other`'s non-empty fields onto a copy of self.

        This is how manual entry patches a partial scrape: whatever the human
        typed wins, everything they left blank keeps the scraped value.
        """
        merged = Listing(**{**asdict(self), "photos": list(self.photos)})
        for key, value in asdict(other).items():
            if key in ("photos", "notes"):
                continue
            if value not in (None, "", []):
                setattr(merged, key, value)
        if other.photos:
            merged.photos = list(other.photos)
        merged.notes = [*self.notes, *other.notes]
        return merged
