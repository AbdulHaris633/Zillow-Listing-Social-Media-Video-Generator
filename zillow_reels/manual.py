"""The fallback path: a template file (or a prompt) when scraping can't get through.

The important behaviour here is `prefill`. When a scrape half-works, we write a
template that already contains what we got and marks what's missing, so the
human types three fields instead of twelve. That keeps the workflow moving
rather than dumping the operator back to square one.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .cards import UNITS_PER_CARD
from .models import Listing, Photo, format_count

INSTRUCTIONS = [
    "Fill in what you can — every field except address, price and photos is optional.",
    "photos: a list of image URLs, local file paths, or {\"url\": ..., \"caption\": \"Kitchen\"} objects.",
    "A local folder path in \"photo_folder\" is expanded to every image inside it, sorted by name.",
    "Then run:  zillow-reels make --manual <this file>",
]

TEMPLATE: dict[str, Any] = {
    "url": "",
    "address": "",
    "street": "",
    "city": "",
    "state": "",
    "zipcode": "",
    "price": "",
    "beds": "",
    "baths": "",
    "sqft": "",
    "year_built": "",
    "home_type": "",
    "status": "FOR_SALE",
    "description": "",
    "agent_name": "",
    "agent_phone": "",
    "brokerage": "",
    "photo_folder": "",
    "photos": [],
    # Rentals only, and pre-filled rather than hand-typed: nobody is going to
    # key in fourteen units, but losing a scraped table because one other
    # field needed correcting would be worse.
    "units": [],
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff"}


def write_template(path: str | Path, listing: Listing | None = None) -> Path:
    """Write a blank or pre-filled template. Never clobbers silently."""
    path = Path(path).expanduser()
    data: dict[str, Any] = dict(TEMPLATE)

    if listing is not None:
        scraped = listing.to_dict()
        for key in data:
            value = scraped.get(key)
            if value not in (None, "", []):
                data[key] = value
        data["address"] = listing.address
        data["photos"] = [p.to_dict() for p in listing.photos]
        missing = listing.missing_required() + listing.missing_optional()
        data["_missing"] = missing
        data["_note"] = (
            f"Pre-filled from a partial scrape ({listing.source or 'unknown source'}). "
            f"Still needed: {', '.join(missing) if missing else 'nothing — ready to render'}."
        )

    data["_instructions"] = INSTRUCTIONS

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def expand_photo_folder(folder: str) -> list[Photo]:
    directory = Path(folder).expanduser()
    if not directory.is_dir():
        raise FileNotFoundError(f"photo_folder does not exist: {directory}")
    files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES and not p.name.startswith(".")
    )
    return [Photo(path=p) for p in files]


def load_manual(path: str | Path) -> Listing:
    """Read a filled-in template into a Listing."""
    path = Path(path).expanduser()
    data = json.loads(path.read_text(encoding="utf-8"))
    data = {k: v for k, v in data.items() if not k.startswith("_")}

    folder = str(data.pop("photo_folder", "") or "").strip()
    listing = Listing.from_dict(data)
    listing.source = f"manual ({path.name})"

    # Resolve relative photo paths against the template's own directory, so a
    # template can sit next to its photo folder and travel as a unit.
    def resolve(photo: Photo) -> Photo:
        # Always canonicalise, including already-absolute paths: on macOS a
        # temp dir reached as /var/... and /private/var/... is the same file,
        # and the two spellings would otherwise fail to match each other.
        if photo.path:
            base = photo.path if photo.path.is_absolute() else path.parent / photo.path
            photo.path = base.resolve()
        return photo

    listed = [resolve(p) for p in listing.photos]

    if folder:
        expanded = [resolve(p) for p in expand_photo_folder(folder)]
        # Entries named explicitly in "photos" usually exist to attach a
        # caption to a file that photo_folder also picks up. Merge those
        # captions onto the folder entry rather than appending a duplicate,
        # which de-duplication would otherwise discard — taking the caption
        # with it.
        captions = {str(p.path): p.caption for p in listed if p.path and p.caption}
        for photo in expanded:
            if not photo.caption:
                photo.caption = captions.get(str(photo.path), "")
        known = {str(p.path) for p in expanded if p.path}
        extra = [p for p in listed if not (p.path and str(p.path) in known)]
        listing.photos = expanded + extra
    else:
        listing.photos = listed

    return listing


# (attribute, label) pairs offered for editing in the review step, in the
# order they read most naturally on screen.
REVIEW_FIELDS = [
    ("address", "Address"),
    ("price", "Price"),
    ("beds", "Beds"),
    ("baths", "Baths"),
    ("sqft", "Sq ft"),
    ("status", "Status"),
    ("year_built", "Year built"),
    ("home_type", "Home type"),
    ("agent_name", "Agent"),
    ("agent_phone", "Agent phone"),
    ("brokerage", "Brokerage"),
    ("description", "Description"),
]


def _display_value(listing: Listing, attr: str, width: int = 66) -> str:
    if attr == "price":
        return listing.price_display or ""
    if attr == "address":
        return listing.address
    # Show what the card will show. A building's beds are "1-2", and a review
    # table reporting the bare "1" would invite a correction to something the
    # video was never going to display.
    if attr in ("beds", "baths", "sqft") and getattr(listing, f"{attr}_text", ""):
        return getattr(listing, f"{attr}_text")
    value = getattr(listing, attr, "")
    if value in (None, ""):
        return ""
    if isinstance(value, float):
        value = format_count(value)
    text = str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def review_listing(listing: Listing, cfg=None) -> Listing:
    """Show what was scraped and let the operator correct it before rendering.

    Enter accepts everything as-is. Anything else is a field number to edit,
    which is far less tedious than being prompted for twelve fields one by one
    when eleven of them are already right.
    """
    while True:
        print("\n" + "─" * 74)
        print("  Scraped listing — check it over before the video is built")
        print("─" * 74)
        for index, (attr, label) in enumerate(REVIEW_FIELDS, 1):
            value = _display_value(listing, attr)
            marker = " " if value else "!"
            print(f" {marker}{index:>3}  {label:<13} {value or '(missing)'}")

        captioned = sum(1 for p in listing.photos if p.caption)
        limit = getattr(cfg, "max_photos", 0) or 0
        photo_note = f"{len(listing.photos)}"
        if limit and len(listing.photos) > limit:
            photo_note += f" (all saved · first {limit} in the video)"
        if captioned:
            photo_note += f" · {captioned} captioned"
        print(f"     p  {'Photos':<13} {photo_note}")

        # Rentals only. The units become their own card(s) in the video, so
        # they belong in the summary of what is about to be rendered.
        if listing.units:
            pages = -(-len(listing.units) // UNITS_PER_CARD)
            note = f"{len(listing.units)}"
            if listing.price_display:
                note += f" · {listing.price_display}"
            note += f" · {pages} card{'s' if pages != 1 else ''} in the video"
            print(f"     u  {'Units':<13} {note}")
        print("─" * 74)

        missing = listing.missing_required()
        if missing:
            print(f"  Still needed before rendering: {', '.join(missing)}")

        prompt = "  Enter = build the video · number = edit · p = photos · "
        prompt += "u = units · " if listing.units else ""
        choice = input(prompt + "q = quit: ").strip()

        if not choice:
            if missing:
                print(f"  Cannot render yet — {', '.join(missing)} still missing.")
                continue
            return listing
        if choice.lower() == "q":
            raise KeyboardInterrupt
        if choice.lower() == "p":
            _review_photos(listing, cfg)
            continue
        if choice.lower() == "u" and listing.units:
            _review_units(listing)
            continue

        if not choice.isdigit() or not 1 <= int(choice) <= len(REVIEW_FIELDS):
            print("  Not a valid choice.")
            continue

        attr, label = REVIEW_FIELDS[int(choice) - 1]
        # Untruncated, but still formatted — a float count would otherwise
        # echo back as "4.0" and invite the operator to retype it as such.
        current = _display_value(listing, attr, width=10_000)
        print(f"\n  Current {label}: {current or '(empty)'}")
        new = input(f"  New {label} (Enter to keep): ").strip()
        if not new:
            continue

        # Route through from_dict so the same parsing and normalisation the
        # scraper gets is applied to typed values too.
        patch = Listing.from_dict({attr: new})
        for field_name in ("street", "city", "state", "zipcode") if attr == "address" else (attr,):
            setattr(listing, field_name, getattr(patch, field_name))
        if attr == "price":
            listing.price, listing.price_text = patch.price, patch.price_text
        # A typed value replaces any scraped range: leaving beds_text at "1-2"
        # after someone corrects Beds to 3 would put the stale range on screen,
        # since the *_text override outranks the number it was derived from.
        if attr in ("beds", "baths", "sqft"):
            setattr(listing, f"{attr}_text", "")


def parse_number_list(text: str, limit: int) -> list[int]:
    """Turn '1,5,3-6' into [1, 5, 3, 4, 5, 6], keeping the order given.

    Order is preserved rather than sorted, because for photo selection the
    order typed *is* the running order the operator wants in the video.
    """
    picked: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk.lstrip("-"):
            try:
                start, end = (int(x) for x in chunk.split("-", 1))
            except ValueError:
                continue
            step = 1 if end >= start else -1
            picked.extend(range(start, end + step, step))
        elif chunk.isdigit():
            picked.append(int(chunk))
    seen: set[int] = set()
    return [n for n in picked if 1 <= n <= limit and not (n in seen or seen.add(n))]


def _review_units(listing: Listing) -> None:
    """Print the availability table exactly as the video will show it.

    Read-only: the rows come straight off Zillow's own table, and a video that
    quotes a rent the listing does not is worse than one that quotes none. Drop
    a unit with `d` if it has gone since the page was cached.
    """
    while True:
        # Mirrors render_units_cards: pages are filled evenly, not to the brim.
        pages = -(-len(listing.units) // UNITS_PER_CARD)
        per_page = -(-len(listing.units) // pages)
        print()
        for index, unit in enumerate(listing.units, 1):
            # A rule where the card breaks, so the split is visible up front.
            if index > 1 and (index - 1) % per_page == 0:
                print(f"   {'─' * 56}")
            details = " · ".join(
                part
                for part in (
                    unit.layout,
                    f"{int(unit.sqft):,} sq ft" if unit.sqft else "",
                    unit.available,
                )
                if part
            )
            print(f"  {index:>3}. {unit.name or '—':<12} {details:<34} {unit.rent_display:>8}")

        print(
            f"\n   {len(listing.units)} unit(s) across {pages} card"
            f"{'s' if pages != 1 else ''} · {per_page} per card"
        )
        choice = input("\n   d 2,4-6 = drop units  ·  Enter = back: ").strip()
        if not choice:
            return
        if not choice.lower().startswith("d"):
            print("   Not a valid choice.")
            continue

        picked = parse_number_list(choice[1:], len(listing.units))
        if not picked:
            print("   Nothing matched.")
            continue
        if len(picked) >= len(listing.units):
            print("   That would remove every unit — keeping at least one.")
            continue
        listing.units = [u for i, u in enumerate(listing.units, 1) if i not in set(picked)]
        before = listing.price_display
        listing.resummarise_units()
        print(f"   Dropped {len(picked)} unit(s).")
        if listing.price_display != before:
            print(f"   Headline updated: {before} → {listing.price_display}")


def _review_photos(listing: Listing, cfg=None) -> None:
    """Choose how many photos go in the video, and which ones, in what order."""
    if not listing.photos:
        print("  No photos.")
        return

    while True:
        in_video = getattr(cfg, "max_photos", 0) or len(listing.photos)
        in_video = min(in_video, len(listing.photos))
        # Laid out in columns: a gallery of 40+ printed one per line pushes the
        # prompt off screen, and the filename hashes it would show are noise.
        cells = []
        for index, photo in enumerate(listing.photos, 1):
            mark = "▶" if index <= in_video else " "
            label = photo.caption or (Path(photo.path).stem[:16] if photo.path else "")
            cells.append(f"{mark}{index:>3}. {label:<17}")

        columns = 3
        rows = (len(cells) + columns - 1) // columns
        print()
        for row in range(rows):
            line = "".join(cells[row + c * rows] for c in range(columns) if row + c * rows < len(cells))
            print(f"   {line.rstrip()}")
        print(f"\n   ▶ = the {in_video} photo(s) in the video "
              f"· {len(listing.photos)} saved in total")

        choice = input(
            "\n   s = see them  ·  n 8 = how many  ·  k 1,5,3 = pick & order  ·  "
            "d 2,4-6 = delete  ·  Enter = back: "
        ).strip()

        if not choice:
            return

        command, _, argument = choice.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command == "s":
            from .photos import build_contact_sheet, open_file

            sheet = Path(tempfile.gettempdir()) / f"zillow-reels-{listing.slug or 'listing'}.jpg"
            result = build_contact_sheet(listing.photos, sheet)
            if result:
                print(f"   opened {result} — the numbers match this list")
                open_file(result)
            else:
                print("   could not build a contact sheet (no readable photos)")

        elif command == "n":
            if not argument.isdigit() or int(argument) < 1:
                print("   give a count, e.g. 'n 8'")
                continue
            count = min(int(argument), len(listing.photos))
            if cfg is not None:
                cfg.max_photos = count
            print(f"   video will use the first {count} photo(s)")

        elif command == "k":
            picked = parse_number_list(argument, len(listing.photos))
            if not picked:
                print("   nothing recognised, e.g. 'k 1,5,3' or 'k 1-6'")
                continue
            # Chosen photos move to the front in the order given, and the
            # video length follows the selection. The rest stay behind them so
            # they are still saved and uploaded, just not in the video.
            chosen = [listing.photos[n - 1] for n in picked]
            rest = [p for i, p in enumerate(listing.photos, 1) if i not in set(picked)]
            listing.photos = chosen + rest
            if cfg is not None:
                cfg.max_photos = len(chosen)
            print(f"   video will use {len(chosen)} photo(s), in the order you gave")

        elif command == "d":
            remove = set(parse_number_list(argument, len(listing.photos)))
            if not remove:
                print("   nothing recognised, e.g. 'd 2,4-6'")
                continue
            kept = [p for i, p in enumerate(listing.photos, 1) if i not in remove]
            if not kept:
                print("   that would delete every photo — ignoring")
                continue
            print(f"   deleted {len(listing.photos) - len(kept)} photo(s) "
                  "(they won't be saved or uploaded either)")
            listing.photos = kept

        else:
            print("   not a valid choice")


def prompt_interactive(prefill: Listing | None = None) -> Listing:
    """Type-it-in fallback for one-off listings. Enter keeps the shown value."""
    prefill = prefill or Listing()
    print("\nManual listing entry — press Enter to keep the [current] value.\n")

    def ask(label: str, current: Any = "") -> str:
        shown = f" [{current}]" if current not in (None, "", []) else ""
        answer = input(f"  {label}{shown}: ").strip()
        return answer or (str(current) if current not in (None, "", []) else "")

    data: dict[str, Any] = {
        "address": ask("Full address", prefill.address),
        "price": ask("Price", prefill.price_display),
        "beds": ask("Beds", prefill.beds or ""),
        "baths": ask("Baths", prefill.baths or ""),
        "sqft": ask("Square feet", prefill.sqft or ""),
        "description": ask("Description", prefill.description),
        "agent_name": ask("Listing agent", prefill.agent_name),
        "agent_phone": ask("Agent phone", prefill.agent_phone),
        "brokerage": ask("Brokerage", prefill.brokerage),
        "url": prefill.url,
    }

    folder = ask("Photo folder (leave blank to paste URLs)")
    listing = Listing.from_dict(data)
    if folder:
        listing.photos = expand_photo_folder(folder)
    elif prefill.photos:
        listing.photos = list(prefill.photos)
    else:
        print("  Paste photo URLs, one per line. Blank line to finish.")
        urls = []
        while (line := input("    ").strip()):
            urls.append(line)
        listing.photos = [p for p in (Photo.from_any(u) for u in urls) if p]

    listing.source = "manual (interactive)"
    return listing
