"""
Build data/wdfw_seasons.json from the WDFW sport fishing pamphlet (TODO-22).

    python -m scripts.build_wdfw_seasons --year 2026-27 [--html-dir DIR] [--out PATH]

Reads the 16 Special Rules pages of the eRegulations pamphlet (fetched, or
saved copies in --html-dir named <slug>.html) and writes every listed water
with its stretches and season rows. conditions/wdfw_seasons.py reads the file.

Run it when WDFW publishes the new pamphlet (in force from July 1), spot-check
a few waters with conditions.wdfw_seasons.status() (tests/test_wdfw_seasons.py
has hand-checked cases to update), commit the JSON and deploy. The page structure is checked: a page without the
water headings fails the build instead of writing a partial file.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conditions.wdfw_regulations import _SUB_PAGES  # noqa: E402
from conditions.wdfw_seasons import DATA_PATH, PAMPHLET_URL, _counties  # noqa: E402

_HEADERS = {"User-Agent": "FlyFishWA/1.0 (+https://waflyfish.com)"}


def parse_page(html: str, slug: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", class_="content-designer")
    if content is None or not content.select("td[class*=table-h4]"):
        raise SystemExit(f"{slug}: no water headings (td.table-h4-*); the page structure changed")
    kind = "lake" if "lakes" in slug else "river"
    entries: list[dict] = []
    cur = reach = None
    species, span_left = None, 0
    for tr in content.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        cls = " ".join(tds[0].get("class") or [])
        if "table-h4" in cls:
            span = tds[0].find("span")
            head = tds[0].get_text(" ", strip=True)
            name = " ".join((span.get_text(" ", strip=True) if span else head.split(" - ")[0]).split())
            cur = {"name": name.rstrip(" -"), "counties": sorted(_counties(head)), "kind": kind,
                   "page": slug, "reaches": []}
            entries.append(cur)
            reach, species, span_left = None, None, 0
            continue
        if cur is None:
            continue
        texts = [" ".join(t.get_text(" ", strip=True).split()) for t in tds]
        if len(tds) == 1 and tds[0].get("colspan") == "3":
            reach = {"desc": texts[0], "rows": []}
            cur["reaches"].append(reach)
            species, span_left = None, 0
            continue
        if reach is None:
            reach = {"desc": "", "rows": []}
            cur["reaches"].append(reach)
        if len(tds) == 3:
            species, dates, rules = texts
            span_left = int(tds[0].get("rowspan") or 1) - 1
        elif len(tds) == 2 and span_left > 0 and species is not None:
            dates, rules = texts
            span_left -= 1
        else:
            continue
        reach["rows"].append({"species": species, "dates": dates, "rules": rules})
    return entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", required=True, help="pamphlet year, e.g. 2026-27 (in force July 1 to June 30)")
    ap.add_argument("--html-dir", type=Path, help="saved pages <slug>.html instead of fetching")
    ap.add_argument("--out", type=Path, default=DATA_PATH)
    args = ap.parse_args()
    if not re.fullmatch(r"\d{4}-\d{2}", args.year):
        raise SystemExit("--year looks like 2026-27")

    entries: list[dict] = []
    with httpx.Client(headers=_HEADERS, timeout=60, follow_redirects=True) as client:
        for slug in _SUB_PAGES:
            if args.html_dir:
                html = (args.html_dir / f"{slug}.html").read_text()
            else:
                r = client.get(f"{PAMPHLET_URL}/{slug}")
                r.raise_for_status()
                html = r.text
                time.sleep(1)
            page = parse_page(html, slug)
            print(f"{slug}: {len(page)} waters")
            entries.extend(page)

    out = {
        "year": args.year,
        "source": PAMPHLET_URL,
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "entries": entries,
    }
    args.out.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    print(f"{len(entries)} waters -> {args.out} ({args.out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
