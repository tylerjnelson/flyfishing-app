# Structural fingerprint: div.content-designer table td.table-h3
# Source: https://www.eregulations.com/washington/fishing/
# Last validated: 2026-04-05
"""
WDFW Fishing Regulations scraper — annual, manually triggered.

Fetches per-water fishing regulations from eregulations.com (the official
WDFW-partnered annual sport fishing pamphlet) and writes them into the
fishing_regs JSONB column and fly_fishing_legal boolean per spot.

Covers 16 sub-pages:
  - Puget Sound & Coastal Rivers: A-C, D-K, L-R, S-Z
  - Columbia Basin Rivers: Columbia River, A-C, D-K, L-N, O-S, T-Z
  - Westside Lakes: A-E, F-P, Q-Z
  - Eastside Lakes: A-F, G-P, Q-Z

Run once after WDFW publishes new regulations (typically December) via
the annual APScheduler job or a direct call from scripts/.

On ScraperStructureError: logs CRITICAL, leaves all existing fishing_regs
unchanged, returns 0.  Circuit breaker does NOT apply.

fishing_regs JSONB schema per spot:
{
  "open_dates":     "Sat. before Memorial Day-Oct. 31" | null,
  "gear":           "selective gear" | "fly fishing only" | "artificial lure only"
                    | "bait only" | "closed" | null,
  "size_limits":    "Min. size 14\"" | null,
  "bag_limits":     "Daily limit 2" | null,
  "special_rules":  "catch-and-release" | null,
  "year_round_closed": false,
  "fly_fishing_legal": true
}
"""

import logging
import re
from collections import defaultdict

import httpx
from bs4 import BeautifulSoup

from exceptions import ScraperStructureError

log = logging.getLogger(__name__)

_BASE_URL = "https://www.eregulations.com/washington/fishing"
_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=5.0, pool=5.0)

# All 16 sub-pages that contain per-water special rules
_SUB_PAGES = [
    "puget-sound-coastal-rivers-special-rules-a-c",
    "puget-sound-coastal-rivers-special-rules-d-k",
    "puget-sound-coastal-rivers-special-rules-l-r",
    "puget-sound-coastal-rivers-special-rules-s-z",
    "columbia-basin-rivers-special-rules-columbia-river",
    "columbia-basin-rivers-special-rules-a-c",
    "columbia-basin-rivers-special-rules-d-k",
    "columbia-basin-rivers-special-rules-l-n",
    "columbia-basin-rivers-special-rules-o-s",
    "columbia-basin-rivers-special-rules-t-z",
    "westside-lakes-special-rules-a-e",
    "westside-lakes-special-rules-f-p",
    "westside-lakes-special-rules-q-z",
    "eastside-lakes-special-rules-a-f",
    "eastside-lakes-special-rules-g-p",
    "eastside-lakes-special-rules-q-z",
]

# Structural fingerprint — must be present on every sub-page
_FINGERPRINT = "div.content-designer table td.table-h3"

# County suffix pattern: "- YAKIMA CO." or "- KING/SNOHOMISH CO."
_COUNTY_RE = re.compile(r"\s*-\s+[A-Z/\s]+\s+CO\..*$")

# Date patterns
_DATE_RE = re.compile(
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2}"
    r"(?:\s*[-–—]\s*"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2})?",
    re.IGNORECASE,
)
_SAT_MEMORIAL_RE = re.compile(
    r"Sat(?:urday)?\.?\s+before\s+Memorial\s+Day\s*[-–—]\s*\S+",
    re.IGNORECASE,
)

# Gear detection patterns
_BAIT_ONLY_RE = re.compile(r"\bbait\s+only\b", re.IGNORECASE)
_FLY_ONLY_RE = re.compile(r"\bfly\s+(?:fishing\s+)?only\b", re.IGNORECASE)
_ARTIFICIAL_ONLY_RE = re.compile(
    r"\bartificial\s+(?:lure\s+)?only\b|\bno\s+bait\b|\bbait\s+prohibited\b",
    re.IGNORECASE,
)
_SELECTIVE_GEAR_RE = re.compile(r"\bselective\s+gear\b", re.IGNORECASE)
_CLOSED_WATERS_RE = re.compile(r"\bCLOSED\s+WATERS\b")
_CATCH_RELEASE_RE = re.compile(r"\bcatch[- ]and[- ]release\b", re.IGNORECASE)

# Size / bag limit patterns
_SIZE_RE = re.compile(r"[Mm]in(?:imum)?\.?\s+size\s+\d+\"?", re.IGNORECASE)
_BAG_RE = re.compile(r"[Dd]aily\s+limit\s+\d+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def parse_wdfw_regulations(
    spot_check_only: list[str] | None = None,
) -> dict[str, dict]:
    """
    Fetch and parse annual WDFW fishing regulations from eregulations.com.

    Returns dict keyed by normalised water body name → fishing_regs dict.
    Raises ScraperStructureError if any page lacks the expected structure.
    """
    results: dict[str, dict] = {}

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for slug in _SUB_PAGES:
            url = f"{_BASE_URL}/{slug}"
            resp = await client.get(url)
            resp.raise_for_status()
            page_results = _parse_page(resp.text, url)
            # Later pages override earlier ones for the same water name
            # (each name should only appear in one alphabetical section)
            results.update(page_results)

    if spot_check_only:
        return {
            name: regs
            for name, regs in results.items()
            if any(t.lower() in name.lower() for t in spot_check_only)
        }

    return results


async def fetch_and_update_regulations(db) -> int:
    """
    Fetch annual regulations and update matching spots in the DB.
    Returns count of spots updated.  Logs CRITICAL and returns 0 on failure.
    """
    try:
        regs = await parse_wdfw_regulations()
    except ScraperStructureError as exc:
        log.critical(
            "wdfw_regs_structure_error",
            extra={"url": exc.url, "detail": exc.detail},
        )
        return 0
    except Exception as exc:
        log.error("wdfw_regs_fetch_error", extra={"error": str(exc)})
        return 0

    if not regs:
        log.warning("wdfw_regs_no_entries_parsed")
        return 0

    return await _apply_regulations_to_db(regs, db)


# ---------------------------------------------------------------------------
# Page parser
# ---------------------------------------------------------------------------

def _parse_page(html: str, url: str) -> dict[str, dict]:
    soup = BeautifulSoup(html, "html.parser")

    # Validate fingerprint
    if not soup.select_one(_FINGERPRINT):
        raise ScraperStructureError(
            source="wdfw_regulations",
            url=url,
            detail=f"Expected '{_FINGERPRINT}' not found — eregulations.com page structure may have changed",
        )

    content = soup.find("div", class_="content-designer")
    results: dict[str, dict] = {}

    # Each water body is represented as a <table> with table-h3 header rows.
    # A single table may contain multiple water bodies (header → rows → next header).
    for table in content.find_all("table"):
        current_name: str | None = None
        # rules_text accumulates all rules text for the current water body
        rules_rows: list[dict] = []  # each: {species, date, rules}

        for row in table.find_all("tr"):
            header_cell = row.find("td", class_="table-h3")
            if header_cell:
                # Save the previous water body before starting a new one
                if current_name:
                    results[current_name] = _build_regs(rules_rows)
                current_name = _clean_name(header_cell.get_text(strip=True))
                rules_rows = []
            elif current_name:
                cells = row.find_all("td")
                if len(cells) == 3:
                    rules_rows.append({
                        "species": cells[0].get_text(strip=True),
                        "date": cells[1].get_text(strip=True),
                        "rules": cells[2].get_text(strip=True),
                    })
                elif len(cells) == 1:
                    # Location description row — include in rules context
                    rules_rows.append({
                        "species": "",
                        "date": "",
                        "rules": cells[0].get_text(strip=True),
                    })

        # Save the last water body in the table
        if current_name:
            results[current_name] = _build_regs(rules_rows)

    return results


def _clean_name(raw: str) -> str:
    """
    Strip county suffix and title-case the water body name.
    'YAKIMA RIVER- BENTON CO.' → 'Yakima River'
    'TEANAWAY RIVER, NORTH FORK- KITTITAS CO.' → 'Teanaway River, North Fork'
    """
    name = _COUNTY_RE.sub("", raw).strip().rstrip("-").strip()
    return name.title()


def _build_regs(rows: list[dict]) -> dict:
    """Aggregate all rule rows for one water body into the fishing_regs schema."""
    all_rules = " ".join(r["rules"] for r in rows if r["rules"])
    all_text = " ".join(f"{r['species']} {r['date']} {r['rules']}" for r in rows)

    # Year-round closed: "CLOSED WATERS" on an "All species" row (not just one reach)
    all_species_rules = " ".join(
        r["rules"] for r in rows
        if "all species" in r["species"].lower()
    )
    year_round_closed = bool(
        _CLOSED_WATERS_RE.search(all_species_rules)
        and not any(
            r["rules"] and not _CLOSED_WATERS_RE.search(r["rules"])
            for r in rows
            if "all species" in r["species"].lower()
        )
    )

    # fly_fishing_legal: False only when explicitly bait-only
    fly_fishing_legal = not bool(_BAIT_ONLY_RE.search(all_rules))

    # gear: most restrictive / most specific descriptor found
    gear = _detect_gear(all_rules)

    # open_dates: first season date found
    open_dates = _extract_date(all_text)

    # size / bag limits from trout-specific rows
    trout_rules = " ".join(
        r["rules"] for r in rows
        if "trout" in r["species"].lower()
    )
    size_limits = _find(trout_rules or all_rules, _SIZE_RE)
    bag_limits = _find(trout_rules or all_rules, _BAG_RE)

    # special rules
    special_parts = []
    if _CATCH_RELEASE_RE.search(all_rules):
        special_parts.append("catch-and-release")
    if _SELECTIVE_GEAR_RE.search(all_rules) and gear != "selective gear":
        special_parts.append("selective gear rules")
    special_rules = "; ".join(special_parts) or None

    return {
        "open_dates": open_dates,
        "gear": gear,
        "size_limits": size_limits,
        "bag_limits": bag_limits,
        "special_rules": special_rules,
        "year_round_closed": year_round_closed,
        "fly_fishing_legal": fly_fishing_legal,
    }


def _detect_gear(rules_text: str) -> str | None:
    if _CLOSED_WATERS_RE.search(rules_text) and not any(
        pat.search(rules_text)
        for pat in [_SELECTIVE_GEAR_RE, _FLY_ONLY_RE, _ARTIFICIAL_ONLY_RE]
    ):
        return "closed"
    if _FLY_ONLY_RE.search(rules_text):
        return "fly fishing only"
    if _SELECTIVE_GEAR_RE.search(rules_text):
        return "selective gear"
    if _ARTIFICIAL_ONLY_RE.search(rules_text):
        return "artificial lure only"
    if _BAIT_ONLY_RE.search(rules_text):
        return "bait only"
    return None


def _extract_date(text: str) -> str | None:
    m = _SAT_MEMORIAL_RE.search(text)
    if m:
        return m.group(0).strip()
    m = _DATE_RE.search(text)
    return m.group(0).strip() if m else None


def _find(text: str, pattern: re.Pattern) -> str | None:
    m = pattern.search(text)
    return m.group(0).strip() if m else None


# ---------------------------------------------------------------------------
# DB update (unchanged from original)
# ---------------------------------------------------------------------------

async def _apply_regulations_to_db(regs: dict[str, dict], db) -> int:
    """
    Match parsed regulation entries to spots by name and update the DB.

    Matching: case-insensitive substring check between spot name and each
    parsed water body name.  On match:
      - Sets fishing_regs JSONB
      - Sets fly_fishing_legal
      - Upgrades WTA spots from unvalidated → probable (§7.1)
    """
    from sqlalchemy import select
    from db.models import Spot

    result = await db.execute(select(Spot))
    spots = result.scalars().all()

    # Build normalised lookup index once
    reg_index = {name.lower(): (name, data) for name, data in regs.items()}

    updated = 0
    for spot in spots:
        spot_lower = spot.name.lower()
        matched_regs = None

        for reg_lower, (_, water_regs) in reg_index.items():
            if reg_lower in spot_lower or spot_lower in reg_lower:
                matched_regs = water_regs
                break

        if matched_regs is None:
            continue

        spot.fishing_regs = matched_regs
        spot.fly_fishing_legal = matched_regs.get("fly_fishing_legal", True)

        if spot.source == "wta" and spot.seed_confidence == "unvalidated":
            spot.seed_confidence = "probable"

        updated += 1

    if updated:
        await db.commit()

    log.info("wdfw_regs_applied", extra={"spots_updated": updated, "entries_parsed": len(regs)})
    return updated
