"""Smoke tests: parsing, the manual fallback, and a real end-to-end render.

Run with:  python -m unittest discover tests
No network access required — the Zillow page is synthesised from the payload
shape Zillow actually ships (gdpClientCache as a JSON *string* nested inside
__NEXT_DATA__), which is the part most likely to break on a redesign.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from zillow_reels.cards import render_units_cards
from zillow_reels.config import Config
from zillow_reels.manual import load_manual, write_template
from zillow_reels.models import Listing, Photo, Unit
from zillow_reels.scrape import looks_blocked, parse_html

PROPERTY = {
    "zpid": 10000001,
    "streetAddress": "142 Maple Ridge Ct",
    "city": "Fairview",
    "state": "MO",
    "zipcode": "65010",
    "price": 389900,
    "bedrooms": 4,
    "bathrooms": 3,
    "livingArea": 2480,
    "yearBuilt": 1998,
    "homeType": "SINGLE_FAMILY",
    "homeStatus": "FOR_SALE",
    "description": "Beautifully maintained home on a quiet cul-de-sac with a fenced yard.",
    "attributionInfo": {
        "agentName": "Jane Realtor",
        "agentPhoneNumber": "(573) 555-0142",
        "brokerName": "Columbia Realty Group",
    },
    "responsivePhotos": [
        {
            "caption": "Kitchen",
            "mixedSources": {
                "jpeg": [
                    {"url": "https://photos.zillowstatic.com/a.jpg", "width": 576},
                    {"url": "https://photos.zillowstatic.com/a-big.jpg", "width": 1536},
                ]
            },
        },
        {
            "caption": "Primary Bedroom",
            "mixedSources": {"jpeg": [{"url": "https://photos.zillowstatic.com/b.jpg", "width": 1536}]},
        },
    ],
}


def zillow_like_html() -> str:
    """__NEXT_DATA__ with the property buried in a JSON-encoded string."""
    cache = {"ForSaleDoubleScrollFullRenderQuery": {"property": PROPERTY}}
    payload = {
        "props": {"pageProps": {"componentProps": {"gdpClientCache": json.dumps(cache)}}},
        "page": "/homedetails",
    }
    return (
        "<html><head><title>142 Maple Ridge Ct | Zillow</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


class TestParsing(unittest.TestCase):
    def test_next_data_extraction(self):
        result = parse_html(zillow_like_html())
        listing = result.listing
        self.assertEqual(listing.address, "142 Maple Ridge Ct, Fairview, MO 65010")
        self.assertEqual(listing.price_display, "$389,900")
        self.assertEqual(listing.beds, 4)
        self.assertEqual(listing.baths, 3)
        self.assertEqual(listing.sqft, 2480)
        self.assertEqual(listing.agent_name, "Jane Realtor")
        self.assertEqual(listing.brokerage, "Columbia Realty Group")
        self.assertEqual(len(listing.photos), 2)
        # Highest-resolution source wins.
        self.assertEqual(listing.photos[0].url, "https://photos.zillowstatic.com/a-big.jpg")
        self.assertEqual(listing.photos[0].caption, "Kitchen")
        self.assertEqual(listing.missing_required(), [])

    def test_json_ld_fallback(self):
        html = """
        <html><body><script type="application/ld+json">
        {"@type": "SingleFamilyResidence",
         "address": {"@type": "PostalAddress", "streetAddress": "12 Oak Ln",
                     "addressLocality": "Austin", "addressRegion": "TX", "postalCode": "78701"},
         "offers": {"@type": "Offer", "price": 725000},
         "image": ["https://example.com/1.jpg"],
         "description": "Downtown condo."}
        </script></body></html>
        """
        listing = parse_html(html).listing
        self.assertEqual(listing.city, "Austin")
        self.assertEqual(listing.price_display, "$725,000")
        self.assertEqual(len(listing.photos), 1)

    def test_block_detection(self):
        self.assertTrue(looks_blocked("<html>px-captcha</html>"))
        self.assertTrue(looks_blocked("<html>ok</html>", status=403))
        self.assertFalse(looks_blocked("<html>a normal listing page</html>", status=200))

    def test_no_data_is_not_a_crash(self):
        result = parse_html("<html><body>nothing here</body></html>")
        self.assertTrue(result.listing.missing_required())


class TestRenderedDom(unittest.TestCase):
    """The saved-from-browser path, against a captured page (details fictionalised)."""

    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "rendered_dom.html"
        cls.listing = parse_html(fixture.read_text(encoding="utf-8")).listing

    def test_core_fields(self):
        self.assertEqual(self.listing.address, "142 Maple Ridge Ct, Fairview, MO 65010")
        self.assertEqual(self.listing.price_display, "$450,000")
        self.assertEqual(self.listing.beds, 4)
        self.assertEqual(self.listing.baths, 4)
        self.assertEqual(self.listing.sqft, 3074)
        self.assertEqual(self.listing.year_built, "1979")
        self.assertEqual(self.listing.missing_required(), [])

    def test_agent_and_broker_split_from_phone_numbers(self):
        self.assertEqual(self.listing.agent_name, "Alex Carter")
        self.assertEqual(self.listing.agent_phone, "573-555-0142")
        self.assertEqual(self.listing.brokerage, "Prairie Oak Realty")

    def test_full_description_not_the_truncated_one(self):
        # The visible paragraph is cut off with an ellipsis; the whole text is
        # carried on the "Show more" button, which parsers reparent out of the
        # <p>, so it has to be found document-wide.
        self.assertGreater(len(self.listing.description), 700)
        self.assertTrue(self.listing.description.endswith("lakefront living in Fairview!"))
        self.assertNotIn("...", self.listing.description)

    def test_photos_carry_room_captions_at_full_resolution(self):
        captions = [p.caption for p in self.listing.photos]
        self.assertEqual(captions.count("Living Room"), 3)
        self.assertEqual(captions.count("Kitchen"), 3)
        self.assertEqual(captions.count("Primary Bedroom"), 2)
        # One photo is served at ten sizes; only the widest should survive.
        self.assertEqual(len(self.listing.photos), 8)
        self.assertTrue(all("1920" in p.url for p in self.listing.photos))


class TestBuildingDom(unittest.TestCase):
    """Apartment/building pages (zillow.com/apartments/...), a second page type."""

    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "building_dom.html"
        cls.listing = parse_html(fixture.read_text(encoding="utf-8")).listing

    def test_address_beats_the_building_name(self):
        # The <h1> is "Willow Bend Estates II". parse_dom's heading fallback
        # reads that as the street, which is the bug this parser exists to fix.
        self.assertEqual(self.listing.address, "3820 N Cedar St, Fairview, MO 65010")
        self.assertEqual(self.listing.city, "Fairview")
        self.assertEqual(self.listing.zipcode, "65010")

    def test_stats_span_the_whole_table_not_the_cheapest_row(self):
        # A building has no single price, bed count or floor area. Quoting only
        # the cheapest unit would advertise "2 bd · 822 sq ft · $1,014" for a
        # building that also rents 3-bed, 1,140 sq ft units at $1,395.
        self.assertEqual(self.listing.price_display, "$1,014-$1,395+/mo")
        self.assertEqual(
            self.listing.stats(),
            [("2-3", "BEDS"), ("1-2", "BATHS"), ("822-1,140", "SQ FT")],
        )

    def test_numeric_fields_keep_the_low_end_for_gating(self):
        # missing_required() and any sorting read the numbers, so they stay
        # numeric; only the display goes to a range.
        self.assertEqual(self.listing.price, 1014)
        self.assertEqual(self.listing.beds, 2)
        self.assertEqual(self.listing.baths, 1)
        self.assertEqual(self.listing.sqft, 822)

    def test_a_stat_every_unit_agrees_on_stays_singular(self):
        # One bath across the board must read "1 BATH", not "1 BATHS".
        single = Listing.from_dict({"baths": 1, "baths_text": "1", "beds": 2, "beds_text": "1-2"})
        self.assertEqual(single.stats(), [("1-2", "BEDS"), ("1", "BATH")])

    def test_every_available_unit_is_kept(self):
        self.assertEqual([u.name for u in self.listing.units], ["604", "3 Bedroom"])
        cheap, dear = self.listing.units
        self.assertEqual((cheap.beds, cheap.baths, cheap.sqft), (2, 1, 822))
        self.assertEqual(cheap.available, "Now")
        self.assertEqual(cheap.rent_display, "$1,014")
        self.assertEqual(dear.layout, "3 bd · 2 ba")

    def test_units_are_ordered_cheapest_first(self):
        # The table arrives sorted by whatever column Zillow last sorted on.
        rents = [u.rent for u in self.listing.units]
        self.assertEqual(rents, sorted(rents))

    def test_management_company_read_from_the_legacy_agent_block(self):
        # The display name is frequently a role rather than a person, which
        # makes the business name beside it the half worth putting on screen.
        self.assertEqual(self.listing.agent_name, "Leasing Agent")
        self.assertEqual(self.listing.brokerage, "Prairie Oak Management")
        self.assertEqual(self.listing.agent_phone, "(573) 555-0142")

    def test_description_skips_the_amenity_card_above_it(self):
        # An amenity card sits between the "What's special" heading and the
        # copy, so the next sibling is the wrong element to read.
        self.assertTrue(self.listing.description.startswith("Willow Bend Estates II is a Senior"))
        self.assertNotIn("Clubhouse", self.listing.description)
        self.assertNotIn("Show more", self.listing.description)

    def test_neighbouring_properties_are_not_scooped_into_the_gallery(self):
        # The page ends with a carousel of other buildings, each with a photo,
        # and the management company has a logo. None belong in this slideshow.
        urls = [p.url for p in self.listing.photos]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("-f_b.jpg" in u for u in urls))
        self.assertFalse(any("-p_i.jpg" in u for u in urls))  # neighbours
        self.assertFalse(any("-r_a.jpg" in u for u in urls))  # agent logo

    def test_renders_without_manual_input(self):
        self.assertEqual(self.listing.missing_required(), [])
        self.assertEqual(self.listing.home_type, "Apartment building")
        self.assertEqual(self.listing.status, "FOR_RENT")


class TestReviewStep(unittest.TestCase):
    """The confirm-before-render prompt."""

    def _review(self, listing, keys, cfg=None):
        """Run the prompt with scripted keystrokes and its output swallowed."""
        import builtins
        import contextlib
        import io

        from zillow_reels.manual import review_listing

        supplied = iter(keys)
        original = builtins.input
        builtins.input = lambda *a, **k: next(supplied)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return review_listing(listing, cfg or Config(max_photos=14))
        finally:
            builtins.input = original

    def _review_with(self, listing, keys, cfg):
        return self._review(listing, keys, cfg)

    def _listing(self):
        listing = Listing.from_dict({
            "address": "1 A St, Reno, NV 89501", "price": 100000, "beds": 3, "baths": 2,
        })
        listing.photos = [Photo(url=f"https://x/p{i}.jpg", caption=c)
                          for i, c in enumerate(["Kitchen", "Den", "", "Bath"], 1)]
        return listing

    def test_bare_enter_accepts_everything_unchanged(self):
        listing = self._listing()
        out = self._review(listing, [""])
        self.assertEqual(out.price_display, "$100,000")
        self.assertEqual(len(out.photos), 4)

    def test_editing_a_field_reparses_the_typed_value(self):
        # "2" selects Price; the typed value goes through the same parsing the
        # scraper's output does, so "$525,000" becomes a number.
        out = self._review(self._listing(), ["2", "$525,000", ""])
        self.assertEqual(out.price, 525000)
        self.assertEqual(out.price_display, "$525,000")

    def test_units_are_listed_and_droppable_from_the_review(self):
        # The units become their own card(s), so the operator has to be able to
        # see them before rendering — and drop one that has since been taken.
        listing = self._listing()
        listing.units = [
            Unit(name=str(600 + i), beds=1, baths=1, sqft=513, available="Now", rent=1000 + i)
            for i in range(4)
        ]
        listing.resummarise_units()
        out = self._review(listing, ["u", "d 1", "", ""])
        self.assertEqual([u.name for u in out.units], ["601", "602", "603"])
        self.assertEqual(out.price_display, "$1,001-$1,003/mo")

    def test_review_refuses_to_drop_every_unit(self):
        listing = self._listing()
        listing.units = [Unit(name="A", rent=900), Unit(name="B", rent=950)]
        out = self._review(listing, ["u", "d 1-2", "", ""])
        self.assertEqual(len(out.units), 2)

    def test_editing_a_stat_clears_its_scraped_range(self):
        # A building scrape leaves beds_text="1-2". Correcting Beds to 3 must
        # drop that, or the stale range keeps winning on the card.
        listing = self._listing()
        listing.beds, listing.beds_text = 1, "1-2"
        out = self._review(listing, ["3", "3", ""])
        self.assertEqual(out.beds, 3)
        self.assertEqual(out.beds_text, "")
        self.assertIn(("3", "BEDS"), out.stats())

    def test_editing_the_address_resplits_its_parts(self):
        out = self._review(self._listing(), ["1", "9 Elm St, Ames, IA 50010", ""])
        self.assertEqual(out.city, "Ames")
        self.assertEqual(out.state, "IA")
        self.assertEqual(out.zipcode, "50010")

    def test_photos_can_be_deleted_by_number_and_range(self):
        # p -> submenu, delete 2 and 3-4, back, accept.
        out = self._review(self._listing(), ["p", "d 2,3-4", "", ""])
        self.assertEqual([p.caption for p in out.photos], ["Kitchen"])

    def test_refuses_to_delete_every_photo(self):
        out = self._review(self._listing(), ["p", "d 1-4", "", ""])
        self.assertEqual(len(out.photos), 4)

    def test_video_photo_count_can_be_set(self):
        cfg = Config(max_photos=14)
        self._review_with(self._listing(), ["p", "n 2", "", ""], cfg)
        self.assertEqual(cfg.max_photos, 2)

    def test_picking_photos_sets_both_order_and_count(self):
        # "k 3,1" puts photo 3 first, photo 1 second, and makes the video 2 long.
        cfg = Config(max_photos=14)
        out = self._review_with(self._listing(), ["p", "k 3,1", "", ""], cfg)
        self.assertEqual(cfg.max_photos, 2)
        self.assertEqual([p.caption for p in out.photos[:2]], ["", "Kitchen"])
        # Unpicked photos stay behind the selection, so they're still saved.
        self.assertEqual(len(out.photos), 4)

    def test_number_list_parsing_keeps_typed_order(self):
        from zillow_reels.manual import parse_number_list

        self.assertEqual(parse_number_list("1,5,3", 10), [1, 5, 3])
        self.assertEqual(parse_number_list("2-4", 10), [2, 3, 4])
        self.assertEqual(parse_number_list("4-2", 10), [4, 3, 2])   # descending
        self.assertEqual(parse_number_list("1,1,2", 10), [1, 2])    # de-duplicated
        self.assertEqual(parse_number_list("9,99", 10), [9])        # out of range dropped
        self.assertEqual(parse_number_list("junk", 10), [])

    def test_enter_is_refused_while_required_fields_are_missing(self):
        listing = Listing.from_dict({"address": "1 A St, Reno, NV 89501"})
        listing.photos = [Photo(url="https://x/p1.jpg")]
        # First Enter is rejected (no price), then the price is supplied.
        out = self._review(listing, ["", "2", "250000", ""])
        self.assertEqual(out.price, 250000)
        self.assertEqual(out.missing_required(), [])


class TestFetchHardening(unittest.TestCase):
    def test_headers_are_self_consistent(self):
        from zillow_reels.scrape import CHROME_BUILDS, build_headers

        for build in CHROME_BUILDS:
            headers = build_headers(build=build)
            version = build[0]
            # A Chrome UA advertising one version while sec-ch-ua claims
            # another is itself a detection signal.
            self.assertIn(f"Chrome/{version}.", headers["User-Agent"])
            self.assertIn(f'v="{version}"', headers["sec-ch-ua"])
            self.assertIn(build[2], headers["sec-ch-ua-platform"])
            for required in ("Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site", "Accept-Language"):
                self.assertIn(required, headers)

    def test_throttle_spaces_consecutive_calls(self):
        import time as _time

        from zillow_reels import scrape as scrape_mod

        scrape_mod._last_request_at = 0.0
        scrape_mod.throttle((0.05, 0.06))
        start = _time.monotonic()
        scrape_mod.throttle((0.05, 0.06))
        self.assertGreaterEqual(_time.monotonic() - start, 0.04)

    def test_block_detection_covers_perimeterx(self):
        self.assertTrue(looks_blocked("<html>Please verify you are a human</html>"))
        self.assertTrue(looks_blocked('<div id="px-captcha"></div>'))


class TestListing(unittest.TestCase):
    def test_address_splitting(self):
        listing = Listing.from_dict({"address": "142 Maple Ridge Ct, Fairview, MO 65010"})
        self.assertEqual(listing.street, "142 Maple Ridge Ct")
        self.assertEqual(listing.state, "MO")
        self.assertEqual(listing.zipcode, "65010")

    def test_manual_overrides_scrape_but_keeps_gaps(self):
        scraped = Listing.from_dict({"address": "1 A St, Reno, NV 89501", "beds": 3, "price": 100})
        typed = Listing.from_dict({"price": 250000, "agent_name": "Pat Broker"})
        merged = scraped.merged_with(typed)
        self.assertEqual(merged.price, 250000)      # typed wins
        self.assertEqual(merged.beds, 3)            # scraped survives
        self.assertEqual(merged.agent_name, "Pat Broker")

    def test_stats_drop_missing_fields(self):
        self.assertEqual(Listing.from_dict({"beds": 2}).stats(), [("2", "BEDS")])
        self.assertEqual(Listing.from_dict({"baths": 2.5}).stats(), [("2.5", "BATHS")])

    def test_folder_name_is_filesystem_safe(self):
        listing = Listing.from_dict({"address": "1/2 Slash: Rd, Bend, OR 97701"})
        self.assertNotIn("/", listing.folder_name)
        self.assertNotIn(":", listing.folder_name)


class TestManualFallback(unittest.TestCase):
    def test_prefilled_template_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial = parse_html(zillow_like_html()).listing
            partial.price = None  # simulate a field the scrape missed
            path = write_template(Path(tmp) / "listing.json", partial)

            data = json.loads(path.read_text())
            self.assertIn("price", data["_missing"])
            self.assertEqual(data["address"], "142 Maple Ridge Ct, Fairview, MO 65010")

            data["price"] = "389900"
            path.write_text(json.dumps(data))
            reloaded = load_manual(path)
            self.assertEqual(reloaded.price_display, "$389,900")
            self.assertEqual(reloaded.missing_required(), [])

    def test_download_keeps_the_whole_gallery_by_default(self):
        """The photo folder is an archive of the listing, not just video input."""
        from zillow_reels.photos import download_photos

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sources = []
            for index in range(12):
                path = tmp_path / f"all{index}.jpg"
                Image.new("RGB", (1600, 1200), (index * 12, 90, 120)).save(path)
                sources.append(Photo(path=path))

            dest = tmp_path / "out"
            kept = download_photos(sources, dest, verbose=False)   # no cap
            self.assertEqual(len(kept), 12)
            self.assertEqual(len(list(dest.iterdir())), 12)

    def test_download_leaves_no_surplus_files_when_capped(self):
        """The photo folder is uploaded to Drive, so over-fetch must clean up."""
        from zillow_reels.photos import download_photos

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sources = []
            for index in range(12):
                path = tmp_path / f"src{index}.jpg"
                Image.new("RGB", (1600, 1200), (index * 10, 90, 120)).save(path)
                sources.append(Photo(path=path))

            dest = tmp_path / "out"
            kept = download_photos(sources, dest, max_photos=4, verbose=False)
            self.assertEqual(len(kept), 4)
            self.assertEqual(len(list(dest.iterdir())), 4)

    def test_explicit_captions_attach_to_folder_photos(self):
        """Naming a file in "photos" captions it rather than duplicating it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            photos = root / "photos"
            photos.mkdir()
            for name in ("01-front.jpg", "02-kitchen.jpg"):
                Image.new("RGB", (1600, 1200), (120, 120, 120)).save(photos / name)

            template = root / "listing.json"
            template.write_text(json.dumps({
                "address": "9 Elm St, Ames, IA 50010",
                "price": "300000",
                "photo_folder": str(photos),
                "photos": [{"path": "photos/02-kitchen.jpg", "caption": "Kitchen"}],
            }))
            listing = load_manual(template)

            self.assertEqual(len(listing.photos), 2)  # not 3 — no duplicate
            captions = {p.path.name: p.caption for p in listing.photos}
            self.assertEqual(captions["02-kitchen.jpg"], "Kitchen")
            self.assertEqual(captions["01-front.jpg"], "")

    def test_photo_folder_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            photos = Path(tmp) / "photos"
            photos.mkdir()
            for name in ("02.jpg", "01.jpg", "notes.txt"):
                (photos / name).write_bytes(b"x")
            template = Path(tmp) / "listing.json"
            template.write_text(json.dumps({
                "address": "9 Elm St, Ames, IA 50010",
                "price": "300000",
                "photo_folder": str(photos),
            }))
            listing = load_manual(template)
            self.assertEqual([p.path.name for p in listing.photos], ["01.jpg", "02.jpg"])


class TestRender(unittest.TestCase):
    """The real thing: photos in, playable MP4 out."""

    def test_end_to_end_render(self):
        from zillow_reels.video import build_video

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            photos = []
            for index, colour in enumerate([(120, 90, 60), (60, 90, 120), (90, 120, 60)]):
                path = tmp_path / f"{index}.jpg"
                Image.new("RGB", (1600, 1200), colour).save(path, quality=90)
                photos.append(Photo(path=path, caption=["Kitchen", "Primary Bedroom", ""][index]))

            listing = Listing.from_dict({
                "address": "142 Maple Ridge Ct, Fairview, MO 65010",
                "price": 389900, "beds": 4, "baths": 3, "sqft": 2480,
                "description": "Beautifully maintained home on a quiet cul-de-sac.",
                "agent_name": "Jane Realtor", "brokerage": "Columbia Realty Group",
            })

            cfg = Config(fps=12, title_seconds=1.0, photo_seconds=1.0, outro_seconds=1.0,
                         crossfade_seconds=0.3, video_preset="ultrafast")
            out = build_video(listing, photos, tmp_path / "out.mp4", cfg, verbose=False)

            self.assertTrue(out.exists())
            self.assertGreater(out.stat().st_size, 20_000)

    def test_photo_segment_endpoints(self):
        """The Ken Burns crop must stay inside the source at both extremes.

        Rounding the oversampled base down to whole pixels used to push the
        crop box a fraction past the edge, which Pillow rejects outright.
        """
        from zillow_reels.video import PhotoSegment

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.jpg"
            Image.new("RGB", (1600, 1200), (100, 100, 100)).save(path)
            cfg = Config()
            for index in range(4):  # covers both zoom directions and both pans
                segment = PhotoSegment(path, cfg, index=index, duration=2.0, caption="Kitchen")
                for t in (0.0, 1.0, 2.0):
                    self.assertEqual(segment.frame(t).shape, (cfg.height, cfg.width, 3))

    def test_music_loops_and_fades(self):
        """A track shorter than the video is looped, and both ends fade."""
        import math
        import struct
        import wave

        from zillow_reels.video import AUDIO_FPS, _build_audio

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.wav"
            with wave.open(str(path), "w") as handle:
                handle.setnchannels(2)
                handle.setsampwidth(2)
                handle.setframerate(AUDIO_FPS)
                handle.writeframes(b"".join(
                    struct.pack("<hh", v, v)
                    for v in (int(12000 * math.sin(2 * math.pi * 220 * i / AUDIO_FPS))
                              for i in range(AUDIO_FPS))  # 1 second of tone
                ))

            cfg = Config(music_path=str(path), music_volume=0.2, music_fade_seconds=1.0)
            built = _build_audio(cfg, duration=5.0)  # 5x longer than the source
            self.assertIsNotNone(built)
            clip, _ = built

            import numpy as np

            def peak(start: float, stop: float) -> float:
                # A window, not a single sample: a sine crosses zero often
                # enough that a point probe proves nothing.
                return float(abs(clip.frame_function(np.linspace(start, stop, 2000))).max())

            self.assertAlmostEqual(clip.duration, 5.0)
            self.assertEqual(clip.nchannels, 2)
            self.assertLess(peak(0.0, 0.02), 0.02)      # faded in from silence
            self.assertGreater(peak(2.0, 3.0), 0.05)    # looped region is audible
            self.assertLess(peak(4.98, 5.0), 0.02)      # faded out to silence

    def test_cards_render_with_missing_fields(self):
        """A listing with only an address must still produce readable cards."""
        from zillow_reels.cards import render_outro_card, render_title_card

        listing = Listing.from_dict({"address": "1 Bare St, Nowhere, KS 66002"})
        cfg = Config()
        for card in (render_title_card(listing, cfg), render_outro_card(listing, cfg)):
            self.assertEqual(card.size, (1080, 1920))


class TestUnitsCard(unittest.TestCase):
    """The availability table slide, for rentals only."""

    @staticmethod
    def _listing(count: int) -> Listing:
        return Listing.from_dict({
            "address": "305 Tiger Ln, Columbia, MO 65203",
            "units": [
                {"name": str(300 + i), "beds": 1, "baths": 1, "sqft": 513,
                 "available": "Now", "rent": 1000 + i}
                for i in range(count)
            ],
        })

    def test_a_for_sale_home_gets_no_units_card(self):
        listing = Listing.from_dict({"address": "1 Bare St, Nowhere, KS 66002"})
        self.assertEqual(render_units_cards(listing, Config()), [])

    def test_short_table_fits_one_card(self):
        cards = render_units_cards(self._listing(6), Config())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].size, (1080, 1920))

    def test_long_table_pages_evenly(self):
        # 10 units read better as 5 + 5 than 8 + 2, which leaves a bare card.
        self.assertEqual(len(render_units_cards(self._listing(10), Config())), 2)
        self.assertEqual(len(render_units_cards(self._listing(14), Config())), 2)
        self.assertEqual(len(render_units_cards(self._listing(20), Config())), 3)

    def test_units_survive_a_template_round_trip(self):
        # The manual template writes units out as dicts; they have to come back
        # as Units or the card silently vanishes from a hand-corrected run.
        original = self._listing(3)
        revived = Listing.from_dict({
            "address": original.address,
            "units": [u.to_dict() for u in original.units],
        })
        self.assertEqual(len(revived.units), 3)
        self.assertEqual(revived.units[0].rent_display, "$1,000")

    def test_dropping_a_unit_moves_the_headline_with_it(self):
        # Otherwise the card advertises a rent nobody can rent — the worst kind
        # of stale, since it is the number a viewer acts on.
        listing = self._listing(4)          # rents 1000..1003
        listing.resummarise_units()
        self.assertEqual(listing.price_display, "$1,000-$1,003/mo")
        listing.units = listing.units[1:-1]  # drop cheapest and priciest
        listing.resummarise_units()
        self.assertEqual(listing.price_display, "$1,001-$1,002/mo")
        self.assertEqual(listing.price, 1001)

    def test_resummarise_keeps_zillows_fee_hedge(self):
        listing = self._listing(3)
        listing.price_text = "$1,000-$1,002+/mo"
        listing.resummarise_units()
        self.assertTrue(listing.price_display.endswith("+/mo"))

    def test_units_survive_a_merge(self):
        # asdict() flattens nested dataclasses; without care the merge would
        # put plain dicts back on the listing and the card would crash.
        scraped = self._listing(4)
        merged = scraped.merged_with(Listing.from_dict({"price": 1234}))
        self.assertEqual(len(merged.units), 4)
        self.assertEqual(merged.units[0].layout, "1 bd · 1 ba")


if __name__ == "__main__":
    unittest.main(verbosity=2)
