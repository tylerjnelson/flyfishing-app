"""
Seed spots from WTA trip reports — Phase 3.

For each WTA trail URL in the curated list:
  1. Fetch recent trip reports via wta_scraper.fetch_wta_reports().
  2. If fishing reports found, match the trail to an existing spot by name similarity.
     - Match found: update wta_trail_url on the existing spot.
     - No match: insert a new spot with source='wta', seed_confidence='unvalidated'.
  3. Spots are upgraded from 'unvalidated' to 'probable' by the annual
     wdfw_regulations.fetch_and_update_regulations() job when a regulations
     match is found.  This script does not upgrade confidence directly.

To add new WTA trail URLs, append to _WTA_SPOTS below.

Usage (from backend/ directory):
  python -m scripts.seed_wta_spots
  python -m scripts.seed_wta_spots --dry-run

Prerequisites:
  - Ollama running (WTA report classifier uses LLM)
  - DATABASE_URL in environment
  - Run seed_spots.py first so confirmed/probable spots are present
"""

import argparse
import asyncio
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Curated WTA trail URLs for known WA fly fishing waters.
# Format: (wta_trail_url, spot_name_hint, spot_type)
# spot_name_hint — used to match against existing spots; falls back to slug.
# ---------------------------------------------------------------------------

_WTA_SPOTS: list[tuple[str, str, str]] = [
    # Yakima drainage
    ("https://www.wta.org/go-hiking/hikes/yakima-river-canyon", "Yakima River", "river"),
    ("https://www.wta.org/go-hiking/hikes/teanaway-river", "Teanaway River", "river"),
    ("https://www.wta.org/go-hiking/hikes/cle-elum-river", "Cle Elum River", "river"),
    # Methow Valley
    ("https://www.wta.org/go-hiking/hikes/methow-river", "Methow River", "river"),
    ("https://www.wta.org/go-hiking/hikes/twisp-river", "Twisp River", "river"),
    ("https://www.wta.org/go-hiking/hikes/chewuch-river", "Chewuch River", "river"),
    ("https://www.wta.org/go-hiking/hikes/early-winters-creek", "Early Winters Creek", "creek"),
    # Wenatchee / Icicle
    ("https://www.wta.org/go-hiking/hikes/icicle-creek", "Icicle Creek", "creek"),
    ("https://www.wta.org/go-hiking/hikes/wenatchee-river", "Wenatchee River", "river"),
    # Snoqualmie / Skykomish
    ("https://www.wta.org/go-hiking/hikes/snoqualmie-river", "Snoqualmie River", "river"),
    ("https://www.wta.org/go-hiking/hikes/skykomish-river", "Skykomish River", "river"),
    # Skagit / Sauk
    ("https://www.wta.org/go-hiking/hikes/sauk-river", "Sauk River", "river"),
    # Olympic Peninsula
    ("https://www.wta.org/go-hiking/hikes/sol-duc-river", "Sol Duc River", "river"),
    ("https://www.wta.org/go-hiking/hikes/hoh-river-trail", "Hoh River", "river"),
    ("https://www.wta.org/go-hiking/hikes/bogachiel-river", "Bogachiel River", "river"),
    # Fly-fishing-only lakes
    ("https://www.wta.org/go-hiking/hikes/chopaka-lake", "Chopaka Lake", "lake"),
    ("https://www.wta.org/go-hiking/hikes/lenice-lake", "Lenice Lake", "lake"),
    ("https://www.wta.org/go-hiking/hikes/nunnally-lake", "Nunnally Lake", "lake"),
    ("https://www.wta.org/go-hiking/hikes/dry-falls-lake", "Dry Falls Lake", "lake"),
    ("https://www.wta.org/go-hiking/hikes/pass-lake", "Pass Lake", "lake"),
]


# ---------------------------------------------------------------------------
# Name matching helpers
# ---------------------------------------------------------------------------

def _slug_to_name(url: str) -> str:
    """Extract a human-readable name from a WTA trail URL slug."""
    slug = url.rstrip("/").split("/")[-1]
    return re.sub(r"[-_]", " ", slug).title()


def _name_matches(existing_name: str, hint: str) -> bool:
    """
    Simple case-insensitive containment check.
    Returns True if either name contains the other (ignoring 'River'/'Lake'/'Creek').
    """
    strip_words = {"river", "creek", "lake", "north", "south", "east", "west", "fork"}

    def _tokens(s: str) -> set[str]:
        return {t.lower() for t in s.split() if t.lower() not in strip_words}

    a = _tokens(existing_name)
    b = _tokens(hint)
    if not a or not b:
        return False
    return bool(a & b)


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

async def _run(dry_run: bool) -> None:
    from sqlalchemy import select

    from conditions.wta_scraper import fetch_wta_reports
    from db.connection import AsyncSessionLocal
    from db.models import Spot

    # Load existing spots for name matching
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Spot))
        existing_spots = list(result.scalars().all())

    updated = 0
    created = 0
    no_reports = 0

    for wta_url, name_hint, spot_type in _WTA_SPOTS:
        log.info("processing_wta_url", extra={"url": wta_url, "hint": name_hint})

        try:
            reports = await fetch_wta_reports(wta_url)
        except Exception as exc:
            log.warning("wta_fetch_failed", extra={"url": wta_url, "error": str(exc)})
            continue

        if not reports:
            log.info("no_fishing_reports", extra={"url": wta_url})
            no_reports += 1
            continue

        log.info("fishing_reports_found", extra={"url": wta_url, "count": len(reports)})

        # Find existing spot by name similarity
        match = next(
            (s for s in existing_spots if _name_matches(s.name, name_hint)),
            None,
        )

        if match:
            if match.wta_trail_url == wta_url:
                log.info("wta_url_already_set", extra={"name": match.name})
                continue

            if not dry_run:
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        result = await session.execute(
                            select(Spot).where(Spot.id == match.id)
                        )
                        s = result.scalar_one()
                        s.wta_trail_url = wta_url
            log.info("updated_wta_url", extra={"name": match.name, "url": wta_url})
            updated += 1
        else:
            # Create new spot at unvalidated tier
            display_name = name_hint or _slug_to_name(wta_url)
            if not dry_run:
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        session.add(Spot(
                            name=display_name,
                            type=spot_type,
                            source="wta",
                            seed_confidence="unvalidated",
                            wta_trail_url=wta_url,
                            fly_fishing_legal=True,  # default; corrected by regulations scraper
                        ))
            log.info("created_wta_spot", extra={"name": display_name, "type": spot_type})
            created += 1

    log.info(
        "seed_wta_complete",
        extra={
            "updated": updated,
            "spots_created": created,
            "no_reports": no_reports,
            "dry_run": dry_run,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed spots from WTA trip reports")
    parser.add_argument("--dry-run", action="store_true", help="Log actions, no DB writes")
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
