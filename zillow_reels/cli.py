"""Command line interface.

    zillow-reels make <url>              scrape, render, upload
    zillow-reels make --manual f.json    render from a filled-in template
    zillow-reels make <url> --from-html page.html
    zillow-reels batch listings.csv      one row per listing
    zillow-reels template f.json         write a blank manual template
    zillow-reels probe <url>             test extraction only, render nothing
    zillow-reels auth                    run the Google Drive OAuth flow
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .models import Listing
from .pipeline import RunOptions, RunResult, run_one


def _common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to config.toml (branding, timing, Drive defaults)")
    parser.add_argument("--out", default="out", help="working directory for photos and video (default: out)")
    parser.add_argument("--no-drive", action="store_true", help="skip the Google Drive upload")
    parser.add_argument("--client-secrets", default="", help="Google OAuth client secrets JSON")
    parser.add_argument("--parent-folder", default="", help="Drive folder ID to create listing folders inside")
    parser.add_argument("--console-auth", action="store_true", help="OAuth without a local browser (SSH)")
    parser.add_argument("--fetch", choices=["auto", "curl", "requests", "browser"], default="auto",
                        help="fetch backend; 'auto' escalates curl_cffi -> requests -> browser")
    parser.add_argument("--show-browser", action="store_true",
                        help="run the browser backend non-headless (gets through more often)")
    parser.add_argument("--proxy", default="", help="proxy URL, e.g. http://user:pass@host:port")
    parser.add_argument("--browser-profile", default="",
                        help="persistent Chromium profile dir; reuses cookies between runs")
    parser.add_argument("--browser-channel", default="",
                        help="drive real installed Chrome instead of bundled Chromium, e.g. 'chrome'")
    parser.add_argument("--solve-challenge", action="store_true",
                        help="open a visible browser and wait for you to clear Zillow's "
                             "human check by hand; pair with --browser-profile to reuse it")
    parser.add_argument("--max-photos", type=int,
                        help="photos used in the video (default 14); all are still saved")
    parser.add_argument("--max-downloads", type=int,
                        help="cap how many photos are saved to disk/Drive (default: the whole gallery)")
    parser.add_argument("--photo-seconds", type=float, help="seconds per photo")
    parser.add_argument("--zoom", type=float,
                        help="Ken Burns motion; 1.0 = still (default), 1.04 gentle, 1.12 strong")
    parser.add_argument("--crossfade", type=float,
                        help="dissolve length in seconds; 0 = hard cuts")
    parser.add_argument("--music", help="background music file (mp3/m4a/wav)")
    parser.add_argument("--logo", help="PNG logo overlaid on the title and outro cards")
    parser.add_argument("--captions", choices=["auto", "off"], help="per-photo caption bars")
    parser.add_argument("--quiet", action="store_true")


def _config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config.load(getattr(args, "config", None))
    for attr, field_name in (
        ("max_photos", "max_photos"),
        ("max_downloads", "max_downloads"),
        ("photo_seconds", "photo_seconds"),
        ("zoom", "zoom"),
        ("crossfade", "crossfade_seconds"),
        ("music", "music_path"),
        ("logo", "logo_path"),
        ("captions", "captions"),
        ("parent_folder", "drive_parent_folder_id"),
    ):
        value = getattr(args, attr, None)
        if value:
            setattr(cfg, field_name, value)
    return cfg


def _options_from_args(args: argparse.Namespace) -> RunOptions:
    return RunOptions(
        url=getattr(args, "url", "") or "",
        manual_path=getattr(args, "manual", "") or "",
        html_file=getattr(args, "from_html", "") or "",
        fetch_backend=args.fetch,
        interactive=getattr(args, "interactive", False),
        workdir=Path(args.out),
        upload=not args.no_drive,
        client_secrets=args.client_secrets,
        parent_folder_id=args.parent_folder,
        console_auth=args.console_auth,
        skip_video=getattr(args, "no_video", False),
        headless=not args.show_browser,
        proxy=getattr(args, "proxy", "") or "",
        profile_dir=getattr(args, "browser_profile", "") or "",
        channel=getattr(args, "browser_channel", "") or "",
        solve_challenge=getattr(args, "solve_challenge", False),
        review=getattr(args, "review", False),
        write_fallback_template=not getattr(args, "no_fallback_template", False),
        verbose=not args.quiet,
    )


def _report(result: RunResult) -> None:
    listing = result.listing
    print()
    if result.status == "needs_input":
        print(f"  NEEDS INPUT  {listing.address or listing.url or 'unknown listing'}")
        if result.template_path:
            print(f"    template: {result.template_path}")
        return
    if result.status == "error":
        print(f"  FAILED  {listing.address or listing.url}")
        for message in result.messages[-3:]:
            print(f"    {message}")
        # A failed run can still have salvaged something. Say where it is
        # rather than leaving the impression that nothing was written.
        if result.details_path:
            print(f"    {'details:':<9}{result.details_path}")
        if result.drive.get("folder_url"):
            print(f"    {'drive:':<9}{result.drive['folder_url']}")
        return

    print(f"  DONE  {listing.address}")
    if result.video_path:
        print(f"    {'video:':<9}{result.video_path}")
    print(f"    {'photos:':<9}{len(result.photo_paths)}")
    if result.details_path:
        print(f"    {'details:':<9}{result.details_path}")
    if result.drive.get("folder_url"):
        print(f"    {'drive:':<9}{result.drive['folder_url']}")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_make(args: argparse.Namespace) -> int:
    if not args.url and not args.manual and not args.from_html:
        print("Provide a Zillow URL, --manual <template.json>, or --from-html <page.html>.", file=sys.stderr)
        return 2

    cfg = _config_from_args(args)
    options = _options_from_args(args)
    options.bucket = "reels"

    print(f"\nZillow Reels {__version__}")
    print(f"  Source: {args.url or args.manual or args.from_html}")
    result = run_one(options, cfg)
    _report(result)
    return 0 if result.ok else (3 if result.status == "needs_input" else 1)


def cmd_sold(args: argparse.Namespace) -> int:
    """Archive a sold listing: photos and closing details to Drive, no video."""
    if not args.url and not args.manual and not args.from_html:
        print("Provide a Zillow URL, --manual <template.json>, or --from-html <page.html>.", file=sys.stderr)
        return 2

    cfg = _config_from_args(args)
    options = _options_from_args(args)
    options.skip_video = True
    # A sold listing has no asking price to advertise, and in non-disclosure
    # states Zillow never publishes what it went for. Requiring one would push
    # every such listing into the manual template for a field that does not
    # exist, so the archive job asks only for an address and photos.
    options.required = ("address", "photos")
    # No video means the facts would otherwise survive only on screen, so they
    # are filed alongside the photos. The prefix keeps closed deals from
    # sorting in among active listings in Drive.
    options.write_details = True
    options.folder_prefix = "Sold"
    options.bucket = "sold"

    print(f"\nZillow Reels {__version__} — sold listing")
    print(f"  Source: {args.url or args.manual or args.from_html}")
    result = run_one(options, cfg)
    _report(result)
    return 0 if result.ok else (3 if result.status == "needs_input" else 1)


def cmd_batch(args: argparse.Namespace) -> int:
    """Process a CSV. Recognised columns: url, manual, plus any Listing field."""
    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 2

    cfg = _config_from_args(args)
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [r for r in csv.DictReader(handle) if any((v or "").strip() for v in r.values())]

    if not rows:
        print("CSV has no usable rows.", file=sys.stderr)
        return 2

    print(f"\nZillow Reels {__version__} — batch of {len(rows)} listing(s)\n")
    results: list[tuple[dict, RunResult]] = []

    for index, row in enumerate(rows, 1):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        label = row.get("url") or row.get("manual") or row.get("address") or f"row {index}"
        print(f"[{index}/{len(rows)}] {label}")

        options = _options_from_args(args)
        options.url = row.get("url", "")
        options.manual_path = row.get("manual", "")
        options.interactive = False  # never block a batch on a prompt
        options.review = False
        options.bucket = "reels"      # a batch renders videos, same as `make`

        # Any column beyond url/manual patches the scraped data for this row.
        extra = {k: v for k, v in row.items() if k not in ("url", "manual") and v}
        folder = extra.pop("photo_folder", "")
        options.overrides = Listing.from_dict(extra) if extra else None
        if folder:
            from .manual import expand_photo_folder

            options.overrides = options.overrides or Listing()
            options.overrides.photos = expand_photo_folder(folder)

        try:
            result = run_one(options, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the batch
            result = RunResult(listing=Listing.from_dict(row), status="error", messages=[str(exc)])
            print(f"  ! {exc}")
        _report(result)
        results.append((row, result))

    report_path = Path(args.out).expanduser() / "batch-report.csv"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["url", "address", "status", "video", "drive_folder", "template", "notes"])
        for row, result in results:
            writer.writerow([
                row.get("url", ""),
                result.listing.address,
                result.status,
                str(result.video_path or ""),
                result.drive.get("folder_url", ""),
                str(result.template_path or ""),
                " | ".join(result.messages[-3:]),
            ])

    done = sum(1 for _, r in results if r.ok)
    needs = sum(1 for _, r in results if r.status == "needs_input")
    print(f"\n{done} rendered, {needs} need manual input, {len(results) - done - needs} failed")
    print(f"Report: {report_path}")
    return 0 if done else 1


def cmd_template(args: argparse.Namespace) -> int:
    from .manual import write_template

    path = write_template(args.path)
    print(f"Template written to {path}")
    print("Fill it in, then run:")
    print(f"  zillow-reels make --manual {path}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """Extraction only — shows exactly what a listing yields, renders nothing."""
    from .scrape import scrape

    result = scrape(
        args.url,
        html_file=args.from_html or None,
        backend=args.fetch,
        headless=not args.show_browser,
        proxy=args.proxy or None,
        profile_dir=args.browser_profile or None,
        channel=args.browser_channel or None,
        solve_challenge=args.solve_challenge,
        save_html=getattr(args, "save_html", "") or None,
    )
    listing = result.listing

    print(f"\nSource:  {result.source or 'none'}")
    print(f"Blocked: {result.blocked}")
    for note in result.notes:
        print(f"  - {note}")

    print("\nExtracted:")
    for label, value in (
        ("address", listing.address),
        ("price", listing.price_display),
        ("beds", listing.beds),
        ("baths", listing.baths),
        ("sqft", listing.sqft),
        ("agent", listing.agent_name),
        ("brokerage", listing.brokerage),
        ("sold date", listing.sold_date),
        ("buyer agent", listing.buyer_agent),
        ("photos", len(listing.photos)),
        ("description", (listing.description[:70] + "…") if len(listing.description) > 70 else listing.description),
    ):
        marker = " " if value not in (None, "", 0) else "!"
        print(f"  {marker} {label:<12} {value if value not in (None, '') else '(missing)'}")

    missing = listing.missing_required()
    print(f"\nRequired fields missing: {', '.join(missing) if missing else 'none — ready to render'}")

    if args.json:
        Path(args.json).write_text(json.dumps(listing.to_dict(), indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 0 if not missing else 3


def cmd_auth(args: argparse.Namespace) -> int:
    from .drive import DriveClient, DriveError

    cfg = Config.load(args.config)
    try:
        client = DriveClient.from_config(cfg, args.client_secrets or None, console=args.console_auth)
        about = client.service.about().get(fields="user(emailAddress)").execute()
        email = about.get("user", {}).get("emailAddress", "unknown")
        print(f"Authorised as {email}")
        print(f"Token stored at {cfg.token_path}")
        return 0
    except DriveError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zillow-reels",
        description="Turn a Zillow listing into a vertical social video, filed in Google Drive.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    make = sub.add_parser("make", help="build one listing video")
    make.add_argument("url", nargs="?", default="", help="Zillow listing URL")
    make.add_argument("--manual", default="", help="filled-in template JSON")
    make.add_argument("--from-html", default="", help="parse a page you saved from your browser")
    make.add_argument("--interactive", action="store_true", help="prompt for anything still missing")
    make.add_argument("--no-fallback-template", action="store_true",
                      help="don't write a manual template on failure (for callers that retry)")
    make.add_argument("--review", action="store_true",
                      help="show the scraped data and let you edit it before rendering")
    make.add_argument("--no-video", action="store_true", help="fetch photos and upload, skip rendering")
    _common_args(make)
    make.set_defaults(func=cmd_make)

    # A sold listing is an archive job, not a marketing one: the photos and the
    # closing details go to Drive, and there is nothing to advertise, so no
    # video is rendered. Same pipeline otherwise.
    sold = sub.add_parser("sold", help="archive a sold listing's photos and details (no video)")
    sold.add_argument("url", nargs="?", default="", help="Zillow sold-listing URL")
    sold.add_argument("--manual", default="", help="filled-in template JSON")
    sold.add_argument("--from-html", default="", help="parse a page you saved from your browser")
    sold.add_argument("--no-fallback-template", action="store_true",
                      help="don't write a manual template on failure (for callers that retry)")
    sold.add_argument("--review", action="store_true",
                      help="show the scraped data and let you edit it before uploading")
    _common_args(sold)
    sold.set_defaults(func=cmd_sold)

    batch = sub.add_parser("batch", help="process a CSV of listings")
    batch.add_argument("csv", help="CSV with a 'url' and/or 'manual' column")
    _common_args(batch)
    batch.set_defaults(func=cmd_batch)

    template = sub.add_parser("template", help="write a blank manual-entry template")
    template.add_argument("path", nargs="?", default="listing.json")
    template.set_defaults(func=cmd_template)

    probe = sub.add_parser("probe", help="show what can be extracted, render nothing")
    probe.add_argument("url", nargs="?", default="")
    probe.add_argument("--from-html", default="")
    probe.add_argument("--fetch", choices=["auto", "curl", "requests", "browser"], default="auto")
    probe.add_argument("--show-browser", action="store_true")
    probe.add_argument("--proxy", default="")
    probe.add_argument("--browser-profile", default="")
    probe.add_argument("--browser-channel", default="")
    probe.add_argument("--save-html", default="",
                       help="write the fetched HTML here, to see what the page actually served")
    probe.add_argument("--solve-challenge", action="store_true")
    probe.add_argument("--json", default="", help="write the extracted listing to this file")
    probe.set_defaults(func=cmd_probe)

    auth = sub.add_parser("auth", help="authorise Google Drive and store the token")
    auth.add_argument("--config", default=None)
    auth.add_argument("--client-secrets", default="")
    auth.add_argument("--console-auth", action="store_true")
    auth.set_defaults(func=cmd_auth)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
