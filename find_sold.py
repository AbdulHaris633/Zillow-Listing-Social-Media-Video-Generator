"""Print every sold/price-history-ish key in a saved Zillow page's JSON.

    .venv/bin/python find_sold.py ~/Desktop/bedford.html
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bs4 import BeautifulSoup

from zillow_reels.scrape import _walk, parse_next_data

html = Path(sys.argv[1]).expanduser().read_text(encoding="utf-8", errors="replace")
soup = BeautifulSoup(html, "lxml")

payloads = []
for tag in soup.find_all("script"):
    text = tag.string or tag.get_text() or ""
    if tag.get("id") == "__NEXT_DATA__" or tag.get("type") == "application/json":
        import json
        try:
            payloads.append(json.loads(text))
        except Exception:
            pass
print(f"JSON script blocks: {len(payloads)}")

WANTED = re.compile(r"sold|priceHistory|dateposted|lastSold", re.I)
hits = {}
for payload in payloads:
    for node in _walk(payload):
        for key, value in node.items():
            if not WANTED.search(key):
                continue
            if isinstance(value, (str, int, float)) and value not in ("", None):
                hits.setdefault(key, set()).add(str(value)[:60])
            elif isinstance(value, list) and value:
                hits.setdefault(f"{key}[]", set()).add(str(value[0])[:200])

if not hits:
    print("\nNothing sold-ish in the JSON at all.")
else:
    print("\nSold-ish keys found:")
    for key in sorted(hits):
        for sample in sorted(hits[key])[:3]:
            print(f"  {key:<28} {sample}")

print("\nWhat parse_next_data extracted:")
listing, _ = parse_next_data(soup)
if listing:
    print(f"  sold_date : {listing.sold_date!r}")
    print(f"  status    : {listing.status!r}")
    print(f"  price     : {listing.price_display!r}")
