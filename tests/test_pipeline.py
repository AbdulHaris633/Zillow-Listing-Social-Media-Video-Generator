"""Smoke tests: parsing, the manual fallback, and a real end-to-end render.

Run with:  python -m unittest discover tests
No network access required — the Zillow page is synthesised from the payload
shape Zillow actually ships (gdpClientCache as a JSON *string* nested inside
__NEXT_DATA__), which is the part most likely to break on a redesign.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from zillow_reels.cards import (
    SOLD_CARD_SIZE,
    render_sold_card,
    render_units_cards,
    sold_headline_location,
)
from zillow_reels.config import Config
from zillow_reels.manual import details_text, load_manual, write_template
from zillow_reels.pipeline import RunOptions, RunResult
from zillow_reels.models import Listing, Photo, Unit
from zillow_reels.photos import _validate
from zillow_reels.scrape import looks_blocked, parse_html, stable_identity

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


class TestSoldDom(unittest.TestCase):
    """Sold / off-market pages, which `./sold` archives without a video."""

    @classmethod
    def setUpClass(cls):
        fixture = Path(__file__).parent / "fixtures" / "sold_dom.html"
        cls.listing = parse_html(fixture.read_text(encoding="utf-8")).listing

    def test_address_survives_the_nbsp(self):
        # The heading is "804 E Lakeview St,&nbsp;Centralia, MO 65240"; a
        # non-breaking space is not \s to every regex, and the split would
        # otherwise leave the city glued to the comma.
        self.assertEqual(self.listing.address, "804 E Lakeview St, Centralia, MO 65240")
        self.assertEqual(self.listing.city, "Centralia")

    def test_newer_bed_bath_sqft_container(self):
        # Off-market pages drop the per-part testids and use two bare spans.
        self.assertEqual(self.listing.beds, 2)
        self.assertEqual(self.listing.baths, 1)
        self.assertEqual(self.listing.sqft, 768)

    def test_at_a_glance_found_by_aria_label(self):
        self.assertEqual(self.listing.year_built, "1947")
        self.assertEqual(self.listing.home_type, "Single Family Residence")
        self.assertEqual(self.listing.lot_size, "6,534 Square Feet Lot")

    def test_both_sides_of_the_sale(self):
        self.assertEqual(self.listing.agent_name, "Griffin Anderson")
        self.assertEqual(self.listing.brokerage, "Iron Gate Real Estate")
        # "Bought with" has no testid — only the label beside it identifies it.
        # The licence number trailing the name is not part of the name.
        self.assertEqual(self.listing.buyer_agent, "Shawna Neuner")
        self.assertEqual(self.listing.buyer_brokerage, "Century 21 Community")

    def test_sold_date_from_the_price_history_row(self):
        # The date leads the row, so the pattern cannot anchor on "Sold".
        self.assertEqual(self.listing.sold_date, "8/6/2026")

    def test_a_missing_sale_price_does_not_block_the_archive(self):
        # Missouri is a non-disclosure state: the page says "Price Unknown"
        # and no sale price is ever published. Requiring one would push every
        # such listing into the manual template for a field that cannot exist.
        self.assertEqual(self.listing.price_display, "")
        self.assertEqual(self.listing.missing_required(), ["price"])
        self.assertEqual(self.listing.missing_required(("address", "photos")), [])

    @staticmethod
    def _history(*rows: str) -> str:
        body = "".join(f'<tr label="{r}"><td>x</td></tr>' for r in rows)
        return f'<h1>1 A St, Brooklyn, NY 11216</h1><table><tbody>{body}</tbody></table>'

    def test_price_history_picks_the_most_recent_sale(self):
        # Each row summarises itself in a `label` attribute, which is steadier
        # than reading cells and distinguishes a sale from its neighbours.
        # Deliberately out of order: picking row one would give 2024.
        listing = parse_html(self._history(
            "Date: 10/15/2024, Event: Sold, Price: $100 (-100%)",
            "Date: 8/11/2026, Event: Sold, Price: $600,000 (+41.2%)",
            "Date: 1/20/2026, Event: Sold, Price: $425,000",
        )).listing
        self.assertEqual(listing.sold_date, "8/11/2026")

    def test_price_history_ignores_events_that_are_not_sales(self):
        listing = parse_html(self._history(
            "Date: 11/18/2022, Event: Listing removed, Price: -- null",
            "Date: 10/27/2022, Event: Listed for rent, Price: $3,200 (+45.5%)",
        )).listing
        self.assertEqual(listing.sold_date, "")

    def test_sale_price_recovered_from_the_history_row(self):
        # The header can read "Price Unknown" while the history still carries
        # the figure — worth having, since it is the whole point of the post.
        listing = parse_html(
            '<span data-testid="price">Price Unknown</span>'
            + self._history("Date: 8/11/2026, Event: Sold, Price: $600,000 (+41.2%)")
        ).listing
        self.assertEqual(listing.price_display, "$600,000")

    def test_a_header_price_still_beats_the_history(self):
        listing = parse_html(
            '<span data-testid="price">$610,000</span>'
            + self._history("Date: 8/11/2026, Event: Sold, Price: $600,000")
        ).listing
        self.assertEqual(listing.price_display, "$610,000")

    def test_prose_sold_date_is_not_mangled_by_ignorecase(self):
        # [A-Z][a-z]+ under re.IGNORECASE matches lowercase too, which turned
        # "August 6, 2026" into "st 6, 2026".
        html = '<h1>1 A St, Ames, IA 50010</h1><span>Sold on August 6, 2026</span>'
        self.assertEqual(parse_html(html).listing.sold_date, "August 6, 2026")

    def test_review_accepts_a_sold_listing_with_no_price(self):
        # The review loop refuses Enter while a required field is blank. With
        # the default rules that is a dead end on a sold listing: price can
        # never be filled, so there is no key that gets you out.
        import builtins
        import contextlib
        import io

        from zillow_reels.manual import review_listing

        supplied = iter([""])
        original = builtins.input
        builtins.input = lambda *a, **k: next(supplied)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out = review_listing(self.listing, Config(), ("address", "photos"))
        finally:
            builtins.input = original
        self.assertEqual(out.address, "804 E Lakeview St, Centralia, MO 65240")

    def test_a_listing_date_is_not_mistaken_for_a_sale(self):
        html = '<h1>1 A St, Ames, IA 50010</h1><span>Listed for sale 8/6/2026</span>'
        self.assertEqual(parse_html(html).listing.sold_date, "")


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


class TestSoldArchive(unittest.TestCase):
    """The `sold` run archives facts and files them apart from live listings.

    Both behaviours are opt-in, because `reel` must keep producing exactly the
    folders it always has — a prefix leaking into it would strand every
    for-sale listing under a new name in Drive.
    """

    SOLD = {
        "address": "1439 Zerega Avenue, Bronx, NY 10462",
        "price": 1060000, "beds": 11, "baths": 3, "sqft": 2260,
        "status": "RECENTLY_SOLD", "year_built": 1901,
        "agent_name": "Emran H. Bhuiyan", "brokerage": "Emran Estates Realty",
        "sold_date": "8/6/2026", "buyer_agent": "Imam Hasan",
        "buyer_brokerage": "Exit Realty DKC",
        "url": "https://www.zillow.com/homedetails/x_zpid/",
    }

    def test_details_text_records_the_closing_facts(self):
        text = details_text(Listing.from_dict(self.SOLD))
        for expected in ("1439 Zerega Avenue", "$1,060,000", "8/6/2026",
                         "Imam Hasan", "Exit Realty DKC", "Emran H. Bhuiyan"):
            self.assertIn(expected, text)
        self.assertIn("Source: https://www.zillow.com/homedetails/x_zpid/", text)
        # The address heads the record; repeating it as a row is noise.
        self.assertEqual(text.count("1439 Zerega Avenue"), 1)

    def test_details_text_omits_blanks_rather_than_asserting_them(self):
        text = details_text(Listing.from_dict({"address": "9 Pine St, Bend, OR 97701"}))
        self.assertNotIn("(missing)", text)
        self.assertNotIn("Sold date", text)      # not a sold listing
        self.assertNotIn("Buyer's agent", text)

    def test_sold_command_opts_into_both(self):
        """Capture what `sold` hands the pipeline, without running a scrape."""
        import zillow_reels.cli as cli

        captured = {}

        def fake_run_one(options, cfg):
            captured["options"] = options
            return RunResult(listing=Listing.from_dict(self.SOLD), status="error")

        args = cli.build_parser().parse_args(["sold", "https://example.com/x_zpid/"])
        import contextlib
        import io

        original = cli.run_one
        cli.run_one = fake_run_one
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                args.func(args)
        finally:
            cli.run_one = original

        options = captured["options"]
        self.assertTrue(options.write_details)
        self.assertEqual(options.folder_prefix, "Sold")
        self.assertTrue(options.skip_video)

    def test_reel_leaves_both_off(self):
        self.assertFalse(RunOptions().write_details)
        self.assertEqual(RunOptions().folder_prefix, "")

    def _run(self, **overrides):
        """Run the pipeline on a fixed listing with acquisition stubbed out."""
        import contextlib
        import io

        import zillow_reels.pipeline as pipeline_mod

        listing = Listing.from_dict(self.SOLD)
        listing.photos = [Photo(url="https://example.invalid/nope.jpg")]
        options = RunOptions(
            skip_video=True, upload=False, required=("address",), verbose=False,
            **overrides,
        )
        original = pipeline_mod.acquire
        pipeline_mod.acquire = lambda o, c: (listing, [], False)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return pipeline_mod.run_one(options, Config())
        finally:
            pipeline_mod.acquire = original

    def test_sold_run_is_identifiable_on_disk(self):
        """The prefix reaches local paths, not just Drive.

        Without it a closed deal sits among the for-sale folders under a bare
        address, indistinguishable from a `reel` run.
        """
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(write_details=True, folder_prefix="Sold", workdir=Path(tmp))
            details = Path(result.details_path)
            self.assertTrue(details.name.startswith("sold-"), details.name)
            self.assertTrue(details.parent.name.startswith("sold-"), details.parent.name)

    def test_reel_paths_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(write_details=False, folder_prefix="", workdir=Path(tmp))
            folders = [p.name for p in Path(tmp).iterdir()]
            self.assertEqual(folders, ["1439-zerega-avenue-bronx-ny-10462"])

    def test_each_command_owns_its_bucket(self):
        """The same address processed both ways must not collide.

        A sold archive and a reel of one listing share a slug; without
        separate buckets the second run writes into the first one's folder.
        """
        with tempfile.TemporaryDirectory() as tmp:
            sold = self._run(bucket="sold", folder_prefix="Sold",
                             write_details=True, workdir=Path(tmp))
            self._run(bucket="reels", workdir=Path(tmp))

            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),
                             ["reels", "sold"])
            self.assertIn("/sold/sold-", str(sold.details_path))
            reels = list((Path(tmp) / "reels").iterdir())
            self.assertEqual([p.name for p in reels],
                             ["1439-zerega-avenue-bronx-ny-10462"])

    def test_bucket_is_opt_in(self):
        """A bare pipeline call still writes straight to workdir."""
        self.assertEqual(RunOptions().bucket, "")

    def test_details_survive_a_photo_failure(self):
        """The record is the deliverable, so a dead photo must not take it.

        This is the case that lost the file in the field: one photo scraped,
        zero downloaded, and the run returned before ever writing the facts.
        """
        import contextlib
        import io

        import zillow_reels.pipeline as pipeline_mod

        listing = Listing.from_dict(self.SOLD)
        listing.photos = [Photo(url="https://example.invalid/nope.jpg")]

        with tempfile.TemporaryDirectory() as tmp:
            options = RunOptions(
                write_details=True, folder_prefix="Sold", skip_video=True,
                upload=False, workdir=Path(tmp), required=("address",),
                verbose=False,
            )
            original = pipeline_mod.acquire
            pipeline_mod.acquire = lambda o, c: (listing, [], False)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    result = pipeline_mod.run_one(options, Config())
            finally:
                pipeline_mod.acquire = original

            self.assertEqual(result.status, "error")     # still reported honestly
            self.assertIsNotNone(result.details_path)
            self.assertTrue(Path(result.details_path).exists())
            self.assertIn("8/6/2026", Path(result.details_path).read_text())


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


class TestStableIdentity(unittest.TestCase):
    """One browser identity per profile, or the challenge never stops.

    A persistent profile sends the same cookies every run. Rolling a fresh
    user-agent each time presents that one visitor as macOS Chrome one minute
    and Windows Chrome the next, which invalidates the clearance token as
    fast as it is granted.
    """

    def test_identity_is_stable_within_a_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = stable_identity(tmp)
            for _ in range(5):
                self.assertEqual(stable_identity(tmp), first)

    def test_identity_still_varies_between_profiles(self):
        seen = set()
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(24):
                profile = Path(tmp) / f"p{index}"
                profile.mkdir()
                build, viewport = stable_identity(profile)
                seen.add((build[0], viewport["width"], viewport["height"]))
        self.assertGreater(len(seen), 1, "every profile drew the same identity")

    def test_identity_matches_the_host_platform(self):
        expected = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
        with tempfile.TemporaryDirectory() as tmp:
            build, _viewport = stable_identity(tmp)
        self.assertEqual(build[2], expected)

    def test_unwritable_profile_still_yields_an_identity(self):
        build, viewport = stable_identity("/proc/nonexistent-and-unwritable")
        self.assertTrue(build[0])
        self.assertIn("width", viewport)


class TestSoldCard(unittest.TestCase):
    """The 'JUST SOLD' graphic — the one piece of the archive meant to be posted."""

    LISTING = Listing.from_dict({
        "address": "2268 Homer Avenue, Bronx, NY 10473",
        "price": 567000, "sold_date": "8/13/2026", "status": "RECENTLY_SOLD",
    })

    def _photo(self, tmp, size=(1600, 1200)):
        path = Path(tmp) / "hero.jpg"
        Image.new("RGB", size, (120, 140, 110)).save(path)
        return path

    def test_renders_at_the_portrait_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            card = render_sold_card(self.LISTING, Config(), self._photo(tmp))
        self.assertEqual(card.size, SOLD_CARD_SIZE)
        self.assertEqual(card.mode, "RGB")

    def test_survives_a_landscape_and_a_portrait_hero(self):
        with tempfile.TemporaryDirectory() as tmp:
            for size in [(1600, 1200), (900, 1600), (1080, 1080)]:
                card = render_sold_card(self.LISTING, Config(), self._photo(tmp, size))
                self.assertEqual(card.size, SOLD_CARD_SIZE)

    def test_missing_fields_drop_their_line_rather_than_crashing(self):
        bare = Listing.from_dict({"address": "9 Pine St, Bend, OR 97701"})
        with tempfile.TemporaryDirectory() as tmp:
            card = render_sold_card(bare, Config(), self._photo(tmp))
        self.assertEqual(card.size, SOLD_CARD_SIZE)

    def test_renders_without_a_photo_at_all(self):
        card = render_sold_card(self.LISTING, Config(), None)
        self.assertEqual(card.size, SOLD_CARD_SIZE)

    def test_location_line_spells_the_state_out(self):
        self.assertEqual(sold_headline_location(self.LISTING), "BRONX, NEW YORK")
        self.assertEqual(
            sold_headline_location(Listing.from_dict({"address": "1 A St, Reno, NV 89501"})),
            "RENO, NEVADA",
        )
        self.assertEqual(sold_headline_location(Listing()), "")

    def test_reel_never_asks_for_one(self):
        self.assertFalse(RunOptions().sold_card)

    def _card_from(self, tmp, card_photo):
        """Run a sold pipeline over three flat-coloured photos, return the hero."""
        import contextlib
        import io

        import zillow_reels.pipeline as pipeline_mod

        colours = [(200, 60, 60), (60, 160, 80), (60, 90, 200)]
        source = Path(tmp) / "src"
        source.mkdir()
        photos = []
        for index, colour in enumerate(colours, 1):
            path = source / f"{index:02d}.jpg"
            Image.new("RGB", (1600, 1200), colour).save(path)
            photos.append(Photo(path=path))

        listing = Listing.from_dict(self.LISTING.to_dict())
        listing.photos = photos
        cfg = Config()
        cfg.card_photo = card_photo

        original = pipeline_mod.acquire
        pipeline_mod.acquire = lambda o, c: (listing, [], False)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = pipeline_mod.run_one(
                    RunOptions(sold_card=True, skip_video=True, upload=False,
                               required=("address",), verbose=False,
                               workdir=Path(tmp) / "out"),
                    cfg,
                )
        finally:
            pipeline_mod.acquire = original
        return Image.open(result.card_path).convert("RGB").getpixel((60, 700))

    def test_the_chosen_photo_is_the_one_used(self):
        for choice, expected in ((1, 200), (2, 60), (3, 60)):
            with tempfile.TemporaryDirectory() as tmp:
                red = self._card_from(tmp, choice)[0]
                self.assertAlmostEqual(red, expected, delta=25, msg=f"card_photo={choice}")
        # Photos 2 and 3 both have a low red channel; separate them on blue.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertGreater(self._card_from(tmp, 3)[2], 150)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertLess(self._card_from(tmp, 2)[2], 130)

    def test_a_number_past_the_end_falls_back_to_the_lead_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertGreater(self._card_from(tmp, 99)[0], 150)   # the red one


class TestPhotoValidation(unittest.TestCase):
    """The size floor rejects thumbnails, not orientations.

    A 576x768 listing photo was being deleted for failing a width>=640 test —
    on a tool whose output is a 1080x1920 vertical video, where portrait
    source material is ideal.
    """

    def _check(self, size):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.jpg"
            Image.new("RGB", size, (120, 140, 160)).save(path)
            keep, _digest, _width, reason = _validate(path)
            return keep, reason

    def test_portrait_photos_are_kept(self):
        for size in [(576, 768), (1080, 1920), (400, 640)]:
            keep, reason = self._check(size)
            self.assertTrue(keep, f"{size} rejected: {reason}")

    def test_landscape_photos_are_kept(self):
        for size in [(768, 576), (1536, 1024), (640, 400)]:
            self.assertTrue(self._check(size)[0], size)

    def test_thumbnails_are_still_rejected(self):
        for size in [(384, 256), (200, 300), (1, 1), (639, 400)]:
            keep, reason = self._check(size)
            self.assertFalse(keep, size)
            self.assertIn("too small", reason)


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
