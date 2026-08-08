"""Download, validate and de-duplicate listing photos.

Zillow galleries routinely contain the same room at two resolutions, floor-plan
scans, and the occasional 1x1 tracking pixel. Everything that reaches the video
renderer has been opened by Pillow, checked for a sane size, and hashed against
the others, so a slideshow never stalls on a broken JPEG.
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, UnidentifiedImageError

from .models import Photo
from .scrape import HEADERS

MIN_WIDTH = 640
MIN_HEIGHT = 400


def _safe_stem(index: int, caption: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", caption).strip("-").lower()[:40]
    return f"{index:02d}-{slug}" if slug else f"{index:02d}"


def _download(photo: Photo, dest: Path, index: int, timeout: int) -> Path | None:
    if photo.path and photo.path.exists():
        target = dest / f"{_safe_stem(index, photo.caption)}{photo.path.suffix.lower()}"
        if photo.path.resolve() != target.resolve():
            shutil.copy2(photo.path, target)
            return target
        return photo.path

    if not photo.url:
        return None

    suffix = Path(photo.url.split("?")[0]).suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        suffix = ".jpg"
    target = dest / f"{_safe_stem(index, photo.caption)}{suffix}"

    response = requests.get(photo.url, headers=HEADERS, timeout=timeout, stream=True)
    response.raise_for_status()
    with target.open("wb") as handle:
        for chunk in response.iter_content(65536):
            handle.write(chunk)
    return target


def _validate(path: Path) -> tuple[bool, str, int]:
    """Return (keep, content_hash, width). Rejects tiny or unreadable images."""
    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            width, height = img.size
            if width < MIN_WIDTH or height < MIN_HEIGHT:
                return False, "", width
            # Hash a downscaled copy so re-encodes of the same photo collide.
            thumb = img.convert("RGB").resize((32, 32))
            digest = hashlib.sha1(thumb.tobytes()).hexdigest()
        return True, digest, width
    except (UnidentifiedImageError, OSError, ValueError):
        return False, "", 0


def build_contact_sheet(
    photos: list[Photo],
    out_path: str | Path,
    *,
    columns: int = 6,
    thumb: tuple[int, int] = (300, 225),
    timeout: int = 15,
    workers: int = 8,
    verbose: bool = True,
) -> Path | None:
    """Tile numbered thumbnails into one image, for choosing photos by eye.

    Picking which shots go in the video from a list of filenames is guesswork;
    this fetches small versions of everything and numbers them to match the
    menu, so the choice can be made by looking.
    """
    from PIL import ImageDraw

    out_path = Path(out_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def grab(index_photo: tuple[int, Photo]):
        index, photo = index_photo
        try:
            if photo.path and Path(photo.path).exists():
                image = Image.open(photo.path)
            else:
                response = requests.get(photo.url, headers=HEADERS, timeout=timeout)
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content))
            image = image.convert("RGB")
            image.thumbnail(thumb, Image.LANCZOS)
            return index, image
        except Exception:  # noqa: BLE001 - a missing thumbnail is not fatal
            return index, None

    if verbose:
        print(f"    fetching {len(photos)} thumbnail(s)…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = sorted(pool.map(grab, enumerate(photos, 1)), key=lambda r: r[0])

    usable = [(i, im) for i, im in results if im is not None]
    if not usable:
        return None

    pad, label_h = 8, 22
    cell_w, cell_h = thumb[0], thumb[1] + label_h
    rows = (len(usable) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * (cell_w + pad) + pad, rows * (cell_h + pad) + pad),
        (18, 21, 28),
    )
    draw = ImageDraw.Draw(sheet)

    for slot, (index, image) in enumerate(usable):
        col, row = slot % columns, slot // columns
        x = pad + col * (cell_w + pad)
        y = pad + row * (cell_h + pad)
        sheet.paste(image, (x + (cell_w - image.width) // 2, y + label_h))
        draw.rectangle((x, y, x + 34, y + label_h - 2), fill=(232, 180, 74))
        draw.text((x + 8, y + 5), str(index), fill=(18, 21, 28))

    sheet.save(out_path, quality=88)
    return out_path


def open_file(path: str | Path) -> None:
    """Open a file in the OS viewer; silently do nothing if that's not possible."""
    import subprocess
    import sys as _sys

    opener = {"darwin": "open", "win32": "start"}.get(_sys.platform, "xdg-open")
    try:
        subprocess.run([opener, str(path)], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def download_photos(
    photos: list[Photo],
    dest_dir: str | Path,
    *,
    max_photos: int | None = None,
    timeout: int = 25,
    workers: int = 6,
    verbose: bool = True,
) -> list[Photo]:
    """Fetch photos into dest_dir, keeping order, dropping junk and duplicates.

    `max_photos=None` keeps the entire gallery — which is the default, because
    the photo folder is an archive of the listing, not just the raw material
    for the slideshow. Trimming to the video's length is the caller's job.
    """
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    # When capped, over-fetch a little so rejects don't leave the set short.
    candidates = photos if max_photos is None else photos[: max(max_photos * 2, max_photos + 4)]

    def task(pair: tuple[int, Photo]) -> tuple[int, Photo, Path | None, str]:
        index, photo = pair
        try:
            return index, photo, _download(photo, dest, index + 1, timeout), ""
        except Exception as exc:  # noqa: BLE001 - one bad photo is not fatal
            return index, photo, None, str(exc)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = sorted(pool.map(task, enumerate(candidates)), key=lambda r: r[0])

    kept: list[Photo] = []
    seen: set[str] = set()
    surplus: list[Path] = []
    for _, photo, path, error in results:
        if max_photos is not None and len(kept) >= max_photos:
            # Over-fetched to cover rejects; anything past the cap is deleted
            # rather than left behind, since this folder is also what gets
            # uploaded to Drive as the listing's photo set.
            if path is not None:
                surplus.append(path)
            continue
        if error or path is None:
            if verbose and error:
                print(f"    ! skipped a photo: {error[:90]}")
            continue
        keep, digest, width = _validate(path)
        if not keep:
            path.unlink(missing_ok=True)
            continue
        if digest in seen:
            path.unlink(missing_ok=True)
            continue
        seen.add(digest)
        kept.append(Photo(url=photo.url, caption=photo.caption, path=path, width=width))

    for path in surplus:
        path.unlink(missing_ok=True)

    if verbose:
        print(f"    kept {len(kept)} photo(s) in {dest}")
    return kept
