"""Card rendering with Pillow.

All text in the video is drawn here, not with moviepy's TextClip — TextClip
needs ImageMagick on moviepy 1.x and changed signature in 2.x, and it's the
most common reason a build like this fails on someone else's machine. Pillow
is already a dependency (we're decoding photos with it) and gives exact
control over wrapping, spacing and the safe margins the platforms crop to.

Layout is two-pass: content is measured into a Stack, then drawn centred in
whatever vertical space is left between the safe margins and the footer. That
keeps a sparse listing (address only) and a full one (price, stats, agent)
both looking deliberate instead of top-heavy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import Config
from .models import Listing

# Instagram/TikTok overlay their own UI on the top ~10% and bottom ~18% of a
# 9:16 frame. Nothing readable goes outside these bounds.
SAFE_TOP = 0.10
SAFE_BOTTOM = 0.82


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (255, 255, 255)


@lru_cache(maxsize=64)
def _font(path: str | None, size: int) -> ImageFont.FreeTypeFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    try:  # Pillow >= 10.1 returns a scalable bundled font
        return ImageFont.load_default(size)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


class Typeset:
    """Font accessors bound to a config, so callers just ask for a size."""

    def __init__(self, cfg: Config):
        self._bold = cfg.resolved_font(bold=True)
        self._regular = cfg.resolved_font(bold=False) or self._bold

    def bold(self, size: int) -> ImageFont.FreeTypeFont:
        return _font(self._bold, size)

    def regular(self, size: int) -> ImageFont.FreeTypeFont:
        return _font(self._regular, size)


def measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 99,
) -> list[str]:
    """Greedy word wrap, ellipsising the last line when it overflows."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if measure(draw, trial, font)[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                break
    if len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines and len(" ".join(lines)) < len(text.rstrip()):
        last = lines[-1]
        while last and measure(draw, last + "…", font)[0] > max_width:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines


class Stack:
    """Centred vertical layout that knows its own height before it draws.

    Items are appended with the gap that follows them; `height` is the total
    with the trailing gap discarded, so a block can be centred exactly.
    """

    def __init__(self, draw: ImageDraw.ImageDraw, center_x: int, max_width: int):
        self.draw = draw
        self.cx = center_x
        self.max_width = max_width
        self._items: list[tuple[float, Callable[[float], None], float]] = []

    def add(self, height: float, render: Callable[[float], None], gap: float = 0) -> None:
        self._items.append((height, render, gap))

    @property
    def height(self) -> float:
        if not self._items:
            return 0
        total = sum(h + g for h, _, g in self._items)
        return total - self._items[-1][2]  # drop the trailing gap

    def render(self, top: float) -> float:
        y = top
        for height, render, gap in self._items:
            render(y)
            y += height + gap
        return y

    # --- content helpers --------------------------------------------------

    def text(
        self,
        text: str,
        font: ImageFont.FreeTypeFont,
        fill,
        *,
        gap: float = 0,
        max_lines: int = 1,
        leading: float = 1.32,
        shrink_to_fit: bool = False,
        font_for_size: Callable[[int], ImageFont.FreeTypeFont] | None = None,
    ) -> None:
        if not text:
            return
        if shrink_to_fit and font_for_size:
            size = font.size
            while size > 40 and measure(self.draw, text, font)[0] > self.max_width:
                size -= 6
                font = font_for_size(size)

        lines = wrap_text(self.draw, text, font, self.max_width, max_lines=max_lines)
        if not lines:
            return
        line_height = font.size * leading

        def render(y: float) -> None:
            for index, line in enumerate(lines):
                self.draw.text((self.cx, y + index * line_height), line, font=font, fill=fill, anchor="ma")

        self.add(line_height * len(lines), render, gap)

    def pill(self, label: str, font: ImageFont.FreeTypeFont, bg, fg, *, gap: float = 0) -> None:
        if not label:
            return
        text_w, text_h = measure(self.draw, label, font)
        pad_x, pad_y = 32, 20
        box_h = text_h + 2 * pad_y

        def render(y: float) -> None:
            box = (self.cx - text_w // 2 - pad_x, y, self.cx + text_w // 2 + pad_x, y + box_h)
            self.draw.rounded_rectangle(box, radius=box_h // 2, fill=bg)
            self.draw.text((self.cx, y + pad_y), label, font=font, fill=fg, anchor="ma")

        self.add(box_h, render, gap)

    def rule(self, width: int, colour, *, thickness: int = 4, gap: float = 0) -> None:
        def render(y: float) -> None:
            self.draw.line(
                [(self.cx - width // 2, y), (self.cx + width // 2, y)], fill=colour, width=thickness
            )

        self.add(thickness, render, gap)

    def columns(
        self,
        entries: list[tuple[str, str]],
        value_font: ImageFont.FreeTypeFont,
        label_font: ImageFont.FreeTypeFont,
        value_fill,
        label_fill,
        *,
        gap: float = 0,
    ) -> None:
        if not entries:
            return
        value_h = value_font.size * 1.15
        label_h = label_font.size * 1.3
        block_h = value_h + 14 + label_h
        column_w = self.max_width / len(entries)
        start = self.cx - (column_w * (len(entries) - 1)) / 2

        def render(y: float) -> None:
            for index, (value, label) in enumerate(entries):
                x = start + index * column_w
                self.draw.text((x, y), value, font=value_font, fill=value_fill, anchor="ma")
                self.draw.text((x, y + value_h + 14), label, font=label_font, fill=label_fill, anchor="ma")
                if index:
                    divider_x = x - column_w / 2
                    self.draw.line(
                        [(divider_x, y + 10), (divider_x, y + block_h - 10)],
                        fill=(255, 255, 255, 60),
                        width=2,
                    )

        self.add(block_h, render, gap)


def fit_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale-and-crop to fill `size` without distortion (CSS object-fit: cover)."""
    target_w, target_h = size
    src_w, src_h = image.size
    if src_w == 0 or src_h == 0:
        return Image.new("RGB", size, (0, 0, 0))
    scale = max(target_w / src_w, target_h / src_h)
    new_size = (max(target_w, int(src_w * scale + 0.5)), max(target_h, int(src_h * scale + 0.5)))
    resized = image.resize(new_size, Image.LANCZOS)
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def open_photo(path: str | Path) -> Image.Image:
    """Open an image, apply EXIF rotation, and normalise to RGB."""
    from PIL import ImageOps

    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")


def _backdrop(cfg: Config, hero: Image.Image | None, extra_dim: int = 0) -> Image.Image:
    """Blurred hero photo behind the card, or a flat brand colour.

    The scrim is a vertical gradient rather than a flat overlay: light enough
    at the top that the photo still reads as a photo, heavier lower down where
    the dense text sits.
    """
    if hero is None:
        return Image.new("RGB", cfg.size, hex_to_rgb(cfg.background))

    blurred = fit_cover(hero, cfg.size).filter(ImageFilter.GaussianBlur(26))

    top_alpha, bottom_alpha = 96, 178
    gradient = Image.new("L", (1, cfg.height))
    for y in range(cfg.height):
        ratio = y / max(cfg.height - 1, 1)
        value = top_alpha + (bottom_alpha - top_alpha) * (ratio**1.25)
        gradient.putpixel((0, y), min(255, int(value) + extra_dim))
    scrim = gradient.resize(cfg.size)

    dark = Image.new("RGB", cfg.size, hex_to_rgb(cfg.background))
    return Image.composite(dark, blurred, scrim)


def _paste_logo(canvas: Image.Image, cfg: Config, bottom_y: int) -> int:
    """Bottom-centred logo. Returns the y above it (unchanged if no logo)."""
    if not cfg.logo_path:
        return bottom_y
    path = Path(cfg.logo_path).expanduser()
    if not path.exists():
        return bottom_y
    try:
        with Image.open(path) as raw:
            logo = raw.convert("RGBA")
    except OSError:
        return bottom_y
    max_w = int(cfg.width * 0.34)
    if logo.width > max_w:
        ratio = max_w / logo.width
        logo = logo.resize((max_w, int(logo.height * ratio)), Image.LANCZOS)
    canvas.alpha_composite(logo, ((cfg.width - logo.width) // 2, bottom_y - logo.height))
    return bottom_y - logo.height - 30


def render_title_card(listing: Listing, cfg: Config, hero: Image.Image | None = None) -> Image.Image:
    """Opening card: status, price, address, and the beds/baths/sqft row."""
    canvas = _backdrop(cfg, hero).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    type_ = Typeset(cfg)

    accent = hex_to_rgb(cfg.accent)
    text_rgb = hex_to_rgb(cfg.text)
    muted = hex_to_rgb(cfg.muted)
    margin = int(cfg.width * 0.09)
    content_w = cfg.width - 2 * margin
    center = cfg.width // 2

    # --- footer first: it fixes the bottom of the content area ------------
    footer_bottom = int(cfg.height * SAFE_BOTTOM) + 60
    footer_top = _paste_logo(canvas, cfg, footer_bottom)
    credit = " · ".join(p for p in (listing.agent_name, listing.brokerage) if p)
    if credit:
        credit_font = type_.regular(30)
        line = wrap_text(draw, credit, credit_font, content_w, max_lines=1)
        if line:
            draw.text((center, footer_top), line[0], font=credit_font, fill=muted, anchor="md")
            footer_top -= 40

    # --- content ----------------------------------------------------------
    stack = Stack(draw, center, content_w)
    stack.pill(listing.eyebrow, type_.bold(34), accent, hex_to_rgb(cfg.background), gap=64)
    stack.text(
        listing.price_display,
        type_.bold(140),
        text_rgb,
        gap=40,
        shrink_to_fit=True,
        font_for_size=type_.bold,
    )
    stack.text(listing.address_line1, type_.bold(58), text_rgb, gap=12, max_lines=2)
    stack.text(listing.address_line2, type_.regular(42), muted, gap=0)

    stats = listing.stats()
    if stats:
        stack.add(0, lambda y: None, gap=52)
        stack.rule(180, accent, gap=54)
        stack.columns(stats, type_.bold(70), type_.regular(28), text_rgb, muted)

    area_top = int(cfg.height * SAFE_TOP)
    area_bottom = footer_top - 50
    start = area_top + max((area_bottom - area_top - stack.height) / 2, 0)
    stack.render(start)

    return canvas.convert("RGB")


# How many units fit on one card and still read on a phone held at arm's
# length. Beyond this the list is split across cards rather than shrunk.
UNITS_PER_CARD = 8


def render_units_cards(
    listing: Listing, cfg: Config, hero: Image.Image | None = None
) -> list[Image.Image]:
    """One card per page of the availability table. Empty for a for-sale home.

    A renter scanning a building wants the actual rows — which unit, how big,
    when it's free, what it costs — not just the range on the title card. Long
    tables page rather than shrink: fourteen units squeezed onto one 1080-wide
    card would be unreadable on the device this is watched on.
    """
    if not listing.units:
        return []

    # Spread evenly rather than filling each card to the brim: ten units read
    # better as 5 + 5 than as 8 + 2, which leaves the last card nearly bare.
    count = -(-len(listing.units) // UNITS_PER_CARD)
    per_page = -(-len(listing.units) // count)
    pages = [listing.units[i : i + per_page] for i in range(0, len(listing.units), per_page)]
    return [
        _render_units_page(listing, cfg, hero, page, index, len(pages))
        for index, page in enumerate(pages, 1)
    ]


def _render_units_page(
    listing: Listing,
    cfg: Config,
    hero: Image.Image | None,
    units: list,
    page: int,
    pages: int,
) -> Image.Image:
    # Text-dense like the outro, so the backdrop is dimmed harder than the
    # title card to keep the rows legible over a photo.
    canvas = _backdrop(cfg, hero, extra_dim=70).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    type_ = Typeset(cfg)

    accent = hex_to_rgb(cfg.accent)
    text_rgb = hex_to_rgb(cfg.text)
    muted = hex_to_rgb(cfg.muted)
    margin = int(cfg.width * 0.09)
    content_w = cfg.width - 2 * margin
    center = cfg.width // 2

    heading = "AVAILABLE UNITS" if pages == 1 else f"AVAILABLE UNITS · {page}/{pages}"
    total = len(listing.units)
    summary = f"{total} unit{'s' if total != 1 else ''}"
    if listing.price_display:
        summary += f" · {listing.price_display}"

    # Tall enough that the divider clears the descenders of the detail line
    # above it; any tighter and the rule reads as an underline.
    row_h = 118
    name_font = type_.bold(46)
    detail_font = type_.regular(30)
    rent_font = type_.bold(46)

    def render_rows(y: float) -> None:
        for index, unit in enumerate(units):
            top = y + index * row_h
            draw.text((margin, top), unit.name or "Unit", font=name_font, fill=text_rgb, anchor="la")
            details = " · ".join(
                part
                for part in (
                    unit.layout,
                    f"{int(unit.sqft):,} sq ft" if unit.sqft else "",
                    unit.available,
                )
                if part
            )
            draw.text((margin, top + 54), details, font=detail_font, fill=muted, anchor="la")
            if unit.rent_display:
                draw.text(
                    (cfg.width - margin, top + 8),
                    unit.rent_display,
                    font=rent_font,
                    fill=accent,
                    anchor="ra",
                )
            if index:
                # Midway between the detail line above and the name below —
                # 26px higher and it underlines the text above it.
                line_y = top - 14
                draw.line(
                    [(margin, line_y), (cfg.width - margin, line_y)],
                    fill=(255, 255, 255, 38),
                    width=2,
                )

    stack = Stack(draw, center, content_w)
    stack.pill(heading, type_.bold(32), accent, hex_to_rgb(cfg.background), gap=34)
    stack.text(summary, type_.regular(34), muted, gap=46)
    stack.rule(180, accent, gap=52)
    stack.add(row_h * len(units), render_rows)

    area_top = int(cfg.height * SAFE_TOP)
    area_bottom = int(cfg.height * SAFE_BOTTOM) + 40
    start = area_top + max((area_bottom - area_top - stack.height) / 2, 0)
    stack.render(start)

    return canvas.convert("RGB")


def render_outro_card(listing: Listing, cfg: Config, hero: Image.Image | None = None) -> Image.Image:
    """Closing card: description excerpt, agent details, call to action."""
    # Text-dense card, so the backdrop is dimmed harder than the title card.
    canvas = _backdrop(cfg, hero, extra_dim=42).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    type_ = Typeset(cfg)

    accent = hex_to_rgb(cfg.accent)
    text_rgb = hex_to_rgb(cfg.text)
    muted = hex_to_rgb(cfg.muted)
    margin = int(cfg.width * 0.10)
    content_w = cfg.width - 2 * margin
    center = cfg.width // 2

    # --- bottom block, drawn upward from the safe margin ------------------
    bottom = int(cfg.height * SAFE_BOTTOM) + 70
    bottom = _paste_logo(canvas, cfg, bottom)

    if cfg.disclaimer:
        small = type_.regular(22)
        for line in reversed(wrap_text(draw, cfg.disclaimer, small, content_w, max_lines=2)):
            draw.text((center, bottom), line, font=small, fill=muted, anchor="md")
            bottom -= 30
        bottom -= 16

    if cfg.cta_text:
        draw.text((center, bottom), cfg.cta_text, font=type_.bold(44), fill=accent, anchor="md")
        bottom -= 76

    contact = [p for p in (listing.agent_phone, listing.brokerage) if p]
    if contact:
        draw.text((center, bottom), " · ".join(contact), font=type_.regular(34), fill=muted, anchor="md")
        bottom -= 54
    if listing.agent_name:
        draw.text((center, bottom), listing.agent_name, font=type_.bold(46), fill=text_rgb, anchor="md")
        bottom -= 60

    # --- heading + body, centred in what's left ---------------------------
    stack = Stack(draw, center, content_w)
    stack.text("ABOUT THIS HOME", type_.bold(36), accent, gap=26)
    stack.rule(140, accent, gap=58)

    if listing.description:
        stack.text(listing.description, type_.regular(38), text_rgb, max_lines=11, leading=1.45)
    else:
        # No description scraped: fall back to the facts we do have.
        facts = [f"{value} {label.title()}" for value, label in listing.stats()]
        if listing.home_type:
            facts.append(listing.home_type.replace("_", " ").title())
        if listing.year_built:
            facts.append(f"Built {listing.year_built}")
        for index, fact in enumerate(facts):
            stack.text(fact, type_.regular(40), text_rgb, gap=0 if index == len(facts) - 1 else 14)

    area_top = int(cfg.height * SAFE_TOP)
    area_bottom = bottom - 60
    start = area_top + max((area_bottom - area_top - stack.height) / 2, 0)
    stack.render(start)

    return canvas.convert("RGB")


def render_caption_overlay(text: str, cfg: Config) -> Image.Image | None:
    """Bottom caption bar for a slideshow photo ('Kitchen', 'Primary Bedroom').

    Returned as a full-frame RGBA layer so the compositor can blend it over a
    moving photo without re-rendering the text every frame.
    """
    label = text.strip()
    if not label:
        return None
    label = label.replace("_", " ").strip().title()

    layer = Image.new("RGBA", cfg.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font = Typeset(cfg).bold(42)

    margin = int(cfg.width * 0.08)
    text_w, text_h = measure(draw, label, font)
    text_w = min(text_w, cfg.width - 2 * margin - 80)

    pad_x, pad_y = 34, 22
    box_h = text_h + 2 * pad_y
    y = int(cfg.height * SAFE_BOTTOM) - box_h
    box = (margin, y, margin + text_w + 2 * pad_x + 12, y + box_h)

    draw.rounded_rectangle(box, radius=18, fill=(*hex_to_rgb(cfg.background), 210))
    draw.rectangle((box[0], box[1], box[0] + 6, box[3]), fill=(*hex_to_rgb(cfg.accent), 255))
    draw.text((box[0] + pad_x + 14, box[1] + pad_y - 2), label, font=font, fill=(*hex_to_rgb(cfg.text), 255))
    return layer
