"""
Seed fishing spots from WA DNR Forest and Trust Lands pages — Phase 4 addition.

Each of the 16 DNR forest land area pages is unstructured prose — no coordinates,
no consistent schema. This script:
  1. Fetches each page's HTML and extracts visible text.
  2. Uses call_json_llm() to identify water bodies and fishing status.
  3. Geocodes extractable water body names via HERE Geocoding API.
  4. Seeds new spots at probable confidence; sets fly_fishing_legal=False
     where the page explicitly states no fishing (e.g. bull trout closures).

seed_confidence='probable', source='dnr'
type inferred from name suffix (creek/river/lake/pond); defaults to 'river'.

DNR pages surface lesser-known waters not in stocking data or curated lists —
backcountry streams, remote forest lakes, and headwater creeks.

HERE geocoding uses the HERE_API_KEY env var (already in config.settings).
Spots that fail geocoding are seeded without coordinates (lat/lon = None).
Run embed_spots.py after this script to generate name embeddings.

Idempotent: skips spots whose name already exists in DB (case-insensitive).
Run AFTER: alembic upgrade head, seed_spots.py
Run BEFORE: embed_spots.py

Prerequisites:
  - Ollama running (LLM extraction uses CHAT_MODEL)
  - DATABASE_URL, HERE_API_KEY in environment

Usage (from backend/ directory):
  python -m scripts.seed_dnr_spots
  python -m scripts.seed_dnr_spots --dry-run
  python -m scripts.seed_dnr_spots --page ahtanum   # single area only
"""

import argparse
import asyncio
import logging
import os
import re
import sys

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# DNR forest land pages
# Slugs from dnr.wa.gov/forest-and-trust-lands/<slug>
# ---------------------------------------------------------------------------

_DNR_PAGES: list[tuple[str, str]] = [
    # (area_key, url)
    ("ahtanum",         "https://dnr.wa.gov/forest-and-trust-lands/ahtanum-state-forest"),
    ("blanchard",       "https://dnr.wa.gov/forest-and-trust-lands/blanchard-whatcom-county-and-nearby-islands"),
    ("capitol",         "https://dnr.wa.gov/forest-and-trust-lands/capitol-state-forest"),
    ("elbe_hills",      "https://dnr.wa.gov/forest-and-trust-lands/elbe-hills-and-tahoma-state-forests"),
    ("elwha",           "https://dnr.wa.gov/forest-and-trust-lands/elwha-watershed"),
    ("green_mountain",  "https://dnr.wa.gov/forest-and-trust-lands/green-mountain-and-tahuya-state-forest"),
    ("klickitat",       "https://dnr.wa.gov/forest-and-trust-lands/klickitat-canyon-community-forest"),
    ("little_pend",     "https://dnr.wa.gov/forest-and-trust-lands/little-pend-oreille-state-forest"),
    ("loomis",          "https://dnr.wa.gov/forest-and-trust-lands/loomis-and-loup-loup-state-forests"),
    ("naneum",          "https://dnr.wa.gov/forest-and-trust-lands/naneum-ridge-state-forest"),
    ("olsen_creek",     "https://dnr.wa.gov/forest-and-trust-lands/olsen-creek-state-forest-and-galbraith-mountain"),
    ("olympic",         "https://dnr.wa.gov/forest-and-trust-lands/olympic-peninsula-forests"),
    ("reiter",          "https://dnr.wa.gov/forest-and-trust-lands/reiter-foothills-and-walker-valley"),
    ("teanaway",        "https://dnr.wa.gov/forest-and-trust-lands/teanaway-community-forest"),
    ("tiger_mountain",  "https://dnr.wa.gov/forest-and-trust-lands/tiger-mountain-and-raging-river-state-forests"),
    ("yacolt",          "https://dnr.wa.gov/forest-and-trust-lands/yacolt-burn-state-forest"),
]

_PAGE_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=5.0, pool=5.0)
_GEOCODE_TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=5.0, pool=5.0)
_GEOCODE_URL = "https://geocode.search.hereapi.com/v1/geocode"


# ---------------------------------------------------------------------------
# HTML fetch + text extraction
# ---------------------------------------------------------------------------

async def _fetch_page_text(url: str, client: httpx.AsyncClient) -> str | None:
    """Fetch a DNR page and extract visible text (strips HTML tags)."""
    try:
        resp = await client.get(url, timeout=_PAGE_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        log.warning(f"  fetch_failed url={url} error={exc}")
        return None

    html = resp.text

    # Strip script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.S | re.I)
    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate to ~4000 chars to stay within LLM context
    if len(text) > 4000:
        text = text[:4000] + "..."

    return text


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """
You are analyzing a Washington State DNR forest land page to extract fishing information.

Page text:
{page_text}

Extract every named water body (river, creek, stream, lake, pond) mentioned in the text.
For each, determine:
- name: the water body name as written
- type: "river", "creek", "lake", or "pond" based on the name
- fishing_allowed: true if fishing is mentioned as allowed or no restriction is stated;
  false ONLY if the text explicitly says "no fishing", fishing is prohibited, or it is
  closed for species protection (e.g. bull trout)
- restriction_note: brief reason if fishing_allowed is false, otherwise null

Return a JSON object with a single key "spots" containing an array.
If no water bodies are mentioned, return {{"spots": []}}.

Example output:
{{
  "spots": [
    {{"name": "Ahtanum Creek", "type": "creek", "fishing_allowed": true, "restriction_note": null}},
    {{"name": "Bird Creek", "type": "creek", "fishing_allowed": false, "restriction_note": "No fishing - bull trout protection"}}
  ]
}}
"""


async def _extract_spots_from_text(page_text: str) -> list[dict]:
    """Use call_json_llm() to extract water body names and fishing status."""
    from llm.client import call_json_llm, CHAT_MODEL

    prompt = _EXTRACTION_PROMPT.format(page_text=page_text)
    result = await call_json_llm(prompt, CHAT_MODEL, default={"spots": []})
    spots = result.get("spots", [])

    # Validate each entry has required fields
    valid = []
    for s in spots:
        if isinstance(s, dict) and s.get("name"):
            valid.append(s)
    return valid


# ---------------------------------------------------------------------------
# HERE geocoding
# ---------------------------------------------------------------------------

async def _geocode(name: str, client: httpx.AsyncClient, here_api_key: str) -> tuple[float | None, float | None]:
    """
    Geocode a water body name via HERE Geocoding API.
    Appends ", Washington State" to improve accuracy.
    Returns (lat, lon) or (None, None) on failure.
    """
    try:
        resp = await client.get(
            _GEOCODE_URL,
            params={
                "q": f"{name}, Washington State",
                "in": "countryCode:USA",
                "apiKey": here_api_key,
            },
            timeout=_GEOCODE_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return None, None
        pos = items[0]["position"]
        lat, lon = float(pos["lat"]), float(pos["lng"])
        # Sanity-check: must be in Washington State bounding box
        if 45.5 <= lat <= 49.1 and -125.0 <= lon <= -116.9:
            return lat, lon
        log.debug(f"  geocode_out_of_bounds name={name!r} lat={lat} lon={lon}")
        return None, None
    except Exception as exc:
        log.debug(f"  geocode_failed name={name!r} error={exc}")
        return None, None


# ---------------------------------------------------------------------------
# Main seeding logic
# ---------------------------------------------------------------------------

async def _run(dry_run: bool, page_filter: str | None) -> None:
    from sqlalchemy import select, text as sa_text
    from db.connection import AsyncSessionLocal
    from db.models import Spot
    from config import settings

    pages = _DNR_PAGES
    if page_filter:
        pages = [(k, u) for k, u in pages if k == page_filter]
        if not pages:
            log.error(f"Unknown page key '{page_filter}'. Valid keys: {[k for k,_ in _DNR_PAGES]}")
            return

    # Load existing spot names for deduplication
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Spot.name))
        existing_names = {row[0].lower() for row in result.all()}

    total_created = 0
    total_skipped = 0
    total_no_fish = 0

    async with httpx.AsyncClient(headers={"User-Agent": "flyfish-app/1.0"}) as http_client:
        for area_key, url in pages:
            log.info(f"Processing DNR page: {area_key} ({url})")

            page_text = await _fetch_page_text(url, http_client)
            if not page_text:
                log.warning(f"  skipped: could not fetch page")
                continue

            extracted = await _extract_spots_from_text(page_text)
            log.info(f"  LLM extracted {len(extracted)} water bodies")

            for entry in extracted:
                name = entry["name"].strip()
                if not name:
                    continue

                if name.lower() in existing_names:
                    log.debug(f"  skip existing: {name!r}")
                    total_skipped += 1
                    continue

                fishing_allowed = entry.get("fishing_allowed", True)
                restriction_note = entry.get("restriction_note")
                spot_type = entry.get("type", "river")
                if spot_type not in ("river", "creek", "lake", "pond", "coastal"):
                    spot_type = "river"
                if spot_type == "pond":
                    spot_type = "lake"  # normalise to DB CHECK constraint values

                if dry_run:
                    status = "ALLOW" if fishing_allowed else "CLOSED"
                    log.info(f"  [DRY RUN] {status} {spot_type} {name!r} ({restriction_note or ''})")
                    existing_names.add(name.lower())
                    total_created += 1
                    if not fishing_allowed:
                        total_no_fish += 1
                    continue

                # Geocode
                lat, lon = await _geocode(name, http_client, settings.here_api_key)
                if lat:
                    log.debug(f"  geocoded: {name!r} → ({lat}, {lon})")
                else:
                    log.debug(f"  geocode_miss: {name!r}")

                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        # Re-check inside transaction to be safe
                        chk = await session.execute(
                            select(Spot).where(sa_text("lower(name) = lower(:n)")).params(n=name)
                        )
                        if chk.scalar_one_or_none():
                            total_skipped += 1
                            continue

                        session.add(Spot(
                            name=name,
                            type=spot_type,
                            latitude=lat,
                            longitude=lon,
                            source="dnr",
                            seed_confidence="probable",
                            is_public=True,
                            fly_fishing_legal=fishing_allowed,
                            min_temp_f=40.0 if fishing_allowed else None,
                        ))

                existing_names.add(name.lower())
                total_created += 1
                if not fishing_allowed:
                    total_no_fish += 1
                    log.info(f"  seeded CLOSED: {name!r} — {restriction_note}")

    log.info(
        f"DNR seed complete — "
        f"created={total_created} skipped={total_skipped} fly_fishing_legal=false={total_no_fish} "
        f"dry_run={dry_run}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed fishing spots from WA DNR forest land pages via LLM extraction"
    )
    parser.add_argument("--dry-run", action="store_true", help="Log actions, no DB writes")
    parser.add_argument(
        "--page",
        metavar="KEY",
        help=f"Process only one page by key. Options: {[k for k,_ in _DNR_PAGES]}",
    )
    args = parser.parse_args()
    asyncio.run(_run(dry_run=args.dry_run, page_filter=args.page))


if __name__ == "__main__":
    main()
