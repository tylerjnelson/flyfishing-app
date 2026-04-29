"""
Populate water_bodies.species_primary for recommendation-pool lakes.

Strategy:
  Two separate sets exist in water_bodies:
    A) Access lakes — proper names ("Badger Lake"), have fishing_spots, no species_primary
    B) Stocking-carrier rows — WDFW abbreviations ("BADGER LK (SPOK)"), no fishing_spots,
       have species_primary from the stocking job

  This script normalizes both name sets and uses word-set Jaccard similarity +
  county matching to link A→B, then copies species_primary across.

  Match tiers:
    EXACT  — normalized word sets identical: auto-apply
    HIGH   — Jaccard ≥ 0.80 + county match: auto-apply
    MEDIUM — Jaccard ≥ 0.60: log for review, do not apply
    LOW    — below 0.60: skip

  Dry-run by default. Pass --apply to write changes.

Usage (from backend/):
  sudo /opt/flyfish/venv/bin/python scripts/populate_species_primary.py
  sudo /opt/flyfish/venv/bin/python scripts/populate_species_primary.py --apply
"""

import argparse
import os
import re
import sys
from difflib import SequenceMatcher

import psycopg2

# ---------------------------------------------------------------------------
# County abbreviation → full name mapping
# ---------------------------------------------------------------------------
_COUNTY_ABBREV = {
    "ADAM": "Adams", "ASOT": "Asotin", "BENT": "Benton", "CHEL": "Chelan",
    "CLAR": "Clark", "COLU": "Columbia", "COWL": "Cowlitz", "DOUG": "Douglas",
    "FERR": "Ferry", "FRAN": "Franklin", "GARFI": "Garfield", "GRAN": "Grant",
    "GRAY": "Grays Harbor", "ISLA": "Island", "JEFF": "Jefferson", "KING": "King",
    "KITS": "Kitsap", "KITT": "Kittitas", "KLIC": "Klickitat", "LEWI": "Lewis",
    "LINC": "Lincoln", "MASO": "Mason", "OKAN": "Okanogan", "PACI": "Pacific",
    "PEND": "Pend Oreille", "PIER": "Pierce", "SAN": "San Juan",
    "SKAG": "Skagit", "SKAM": "Skamania", "SNOH": "Snohomish", "SPOK": "Spokane",
    "STEV": "Stevens", "THUR": "Thurston", "WALL": "Wahkiakum", "WALL2": "Walla Walla",
    "WHAT": "Whatcom", "WHIT": "Whitman", "YAKI": "Yakima",
}

# Abbreviation expansions for name normalisation
_EXPANSIONS = {
    r'\bLK\b': 'lake',
    r'\bPD\b': 'pond',
    r'\bRES\b': 'reservoir',
    r'\bCR\b': 'creek',
    r'\bRVR\b': 'river',
    r'\bN\b': 'north',
    r'\bS\b': 'south',
    r'\bE\b': 'east',
    r'\bW\b': 'west',
    r'\bMT\b': 'mountain',
    r'\bST\b': 'saint',
    r'\bLWR\b': 'lower',
    r'\bUPR\b': 'upper',
}


def normalize_name(name: str) -> tuple[frozenset, str]:
    """
    Returns (word_set, cleaned_string) for fuzzy matching.
    Strips county codes, number codes, expands abbreviations, lowercases.
    """
    s = name.upper()
    # Remove county codes: (SPOK), (PIER), (KITS/MASON), (PEND/STEV)
    s = re.sub(r'\([A-Z/]+\)', '', s)
    # Remove trailing number codes: (22), (57)
    s = re.sub(r'\(\d+\)', '', s)
    # Remove @-range suffixes: @P558, @27.0173toend
    s = re.sub(r'@\S+', '', s)
    # Apply expansions (case-insensitive on uppercased string)
    for pattern, replacement in _EXPANSIONS.items():
        s = re.sub(pattern, replacement.upper(), s, flags=re.IGNORECASE)
    # Lowercase, strip punctuation except spaces
    s = re.sub(r'[^a-z0-9\s]', ' ', s.lower())
    words = frozenset(w for w in s.split() if len(w) > 1)
    cleaned = ' '.join(sorted(words))
    return words, cleaned


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def county_matches(access_county: str | None, stocking_name: str) -> bool:
    """Check if any county abbreviation in the stocking name maps to the access county."""
    if not access_county:
        return True  # no county info → don't penalise
    codes = re.findall(r'\(([A-Z/]+)\)', stocking_name.upper())
    if not codes:
        return True
    access_lower = access_county.lower()
    for code_str in codes:
        for part in code_str.split('/'):
            full = _COUNTY_ABBREV.get(part, '').lower()
            if full and full in access_lower:
                return True
    return False


def fetch_stocking_names(years: list[int]) -> dict[str, list[str]]:
    """
    Fetch WDFW stocking API for given years. Returns {UPPER(release_location): [species...]}.
    Uses httpx sync client — runs outside the async event loop.
    """
    import httpx
    url = "https://data.wa.gov/resource/6fex-3r7d.json"
    name_species: dict[str, set] = {}
    for year in years:
        offset = 0
        page_size = 1000
        while True:
            params = {
                "$limit": page_size,
                "$offset": offset,
                "$where": f"release_year='{year}'",
                "$select": "release_location,species",
                "$order": ":id",
            }
            try:
                resp = httpx.get(url, params=params, timeout=30)
                resp.raise_for_status()
                page = resp.json()
            except Exception as exc:
                print(f"  API fetch failed for {year} offset {offset}: {exc}")
                break
            for r in page:
                loc = (r.get("release_location") or "").strip().upper()
                sp = (r.get("species") or "").strip()
                if loc and sp:
                    name_species.setdefault(loc, set()).add(sp)
            if len(page) < page_size:
                break
            offset += page_size
        print(f"  Fetched {year}: {sum(len(v) for v in name_species.values())} species entries across {len(name_species)} locations")
    return {k: sorted(v) for k, v in name_species.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', help='Write changes to DB')
    parser.add_argument('--years', type=int, nargs='+', default=[2024, 2025, 2026],
                        help='WDFW stocking years to fetch (default: 2024 2025 2026)')
    args = parser.parse_args()

    db_url = os.environ.get('DATABASE_URL')
    if not db_url:
        raise RuntimeError('DATABASE_URL environment variable is not set')
    # Convert SQLAlchemy URL to psycopg2 format
    db_url = db_url.replace('postgresql+asyncpg://', 'postgresql://')

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()

    # --- Load access lakes (recommendation pool, no species) ---
    cur.execute("""
        SELECT DISTINCT wb.id, wb.name, wb.county
        FROM water_bodies wb
        JOIN fishing_spots fs ON fs.water_body_id = wb.id
        WHERE wb.type = 'lake'
          AND (wb.species_primary IS NULL OR wb.species_primary = '{}')
        ORDER BY wb.name
    """)
    access_lakes = cur.fetchall()  # (id, name, county)

    # --- Load stocking-carrier rows (have species, no fishing spot) ---
    cur.execute("""
        SELECT wb.id, wb.name, wb.species_primary, wb.county
        FROM water_bodies wb
        WHERE wb.type = 'lake'
          AND wb.species_primary IS NOT NULL
          AND wb.species_primary != '{}'
          AND wb.id NOT IN (
              SELECT DISTINCT water_body_id FROM fishing_spots
              WHERE water_body_id IS NOT NULL
          )
        ORDER BY wb.name
    """)
    stocking_carriers = cur.fetchall()  # (id, name, species_primary, county)

    print(f"Access lakes needing species: {len(access_lakes)}")
    print(f"Stocking-carrier rows with species: {len(stocking_carriers)}")
    print()

    # Pre-compute normalised stocking names
    stocking_norm = [
        (row[0], row[1], row[2], row[3], *normalize_name(row[1]))
        for row in stocking_carriers
    ]  # (id, name, species, county, word_set, cleaned)

    exact = []
    high = []
    medium = []
    unmatched = []

    for acc_id, acc_name, acc_county in access_lakes:
        acc_words, acc_cleaned = normalize_name(acc_name)

        best_score = 0.0
        best_match = None

        for stk_id, stk_name, stk_species, stk_county, stk_words, stk_cleaned in stocking_norm:
            j = jaccard(acc_words, stk_words)
            if j < 0.50:
                continue
            # Sequence match on cleaned strings for tiebreaking
            seq = SequenceMatcher(None, acc_cleaned, stk_cleaned).ratio()
            combined = (j * 0.7) + (seq * 0.3)

            # County bonus
            county_ok = county_matches(acc_county, stk_name)
            if county_ok:
                combined += 0.05

            if combined > best_score:
                best_score = combined
                best_match = (stk_id, stk_name, stk_species, county_ok)

        if best_match is None:
            unmatched.append(acc_name)
            continue

        stk_id, stk_name, stk_species, county_ok = best_match
        acc_words2, _ = normalize_name(acc_name)
        stk_words2, _ = normalize_name(stk_name)

        is_exact = acc_words2 == stk_words2

        entry = (acc_id, acc_name, acc_county, stk_name, stk_species, round(best_score, 3), county_ok)

        if is_exact:
            exact.append(entry)
        elif best_score >= 0.80 and county_ok:
            high.append(entry)
        elif best_score >= 0.60:
            medium.append(entry)
        else:
            unmatched.append(acc_name)

    # --- Report ---
    print(f"EXACT matches (auto-apply):  {len(exact)}")
    print(f"HIGH matches (auto-apply):   {len(high)}")
    print(f"MEDIUM matches (review only): {len(medium)}")
    print(f"Unmatched:                   {len(unmatched)}")
    print()

    auto_apply = exact + high

    if auto_apply:
        print("=== AUTO-APPLY ===")
        for acc_id, acc_name, acc_county, stk_name, species, score, county_ok in auto_apply:
            species_list = list(species) if species else []
            flag = "EXACT" if score >= 0.99 else "HIGH"
            print(f"  [{flag} {score}] {acc_name} ({acc_county}) ← {stk_name} → {species_list}")
            if args.apply:
                cur.execute(
                    "UPDATE water_bodies SET species_primary = %s WHERE id = %s",
                    (species_list, acc_id),
                )
        print()

    if medium:
        print("=== MEDIUM — review before applying ===")
        for acc_id, acc_name, acc_county, stk_name, species, score, county_ok in medium:
            county_str = "county-ok" if county_ok else "county-MISMATCH"
            print(f"  [{score} {county_str}] {acc_name} ({acc_county}) ← {stk_name} → {list(species)}")
        print()

    if unmatched:
        print(f"=== UNMATCHED ({len(unmatched)}) — no stocking data; manual curation needed ===")
        for name in unmatched[:30]:
            print(f"  {name}")
        if len(unmatched) > 30:
            print(f"  ... and {len(unmatched) - 30} more")
        print()

    if args.apply:
        conn.commit()
        print(f"Pass 1 applied {len(auto_apply)} updates.")
    else:
        conn.rollback()
        print("Dry run (pass 1) — pass --apply to write changes.")

    # --- Pass 2: API fetch for remaining unmatched lakes ---
    cur.execute("""
        SELECT DISTINCT wb.id, wb.name, wb.county
        FROM water_bodies wb
        JOIN fishing_spots fs ON fs.water_body_id = wb.id
        WHERE wb.type = 'lake'
          AND (wb.species_primary IS NULL OR wb.species_primary = '{}')
        ORDER BY wb.name
    """)
    still_unmatched = cur.fetchall()

    if not still_unmatched:
        print("All lakes matched in pass 1.")
        cur.close()
        conn.close()
        return

    print(f"\n--- Pass 2: API fetch for {len(still_unmatched)} remaining lakes ---")
    print(f"Fetching WDFW stocking data for years: {args.years}")
    api_data = fetch_stocking_names(args.years)
    print(f"Total unique release locations from API: {len(api_data)}")
    print()

    # Pre-normalise API keys
    api_norm = [
        (loc, species, *normalize_name(loc))
        for loc, species in api_data.items()
    ]  # (original_loc, species_list, word_set, cleaned)

    api_exact = []
    api_high = []
    api_medium = []
    api_unmatched = []

    for acc_id, acc_name, acc_county in still_unmatched:
        acc_words, acc_cleaned = normalize_name(acc_name)

        best_score = 0.0
        best_match = None

        for loc, species, loc_words, loc_cleaned in api_norm:
            j = jaccard(acc_words, loc_words)
            if j < 0.50:
                continue
            seq = SequenceMatcher(None, acc_cleaned, loc_cleaned).ratio()
            combined = (j * 0.7) + (seq * 0.3)
            county_ok = county_matches(acc_county, loc)
            if county_ok:
                combined += 0.05
            if combined > best_score:
                best_score = combined
                best_match = (loc, species, county_ok)

        if best_match is None:
            api_unmatched.append(acc_name)
            continue

        loc, species, county_ok = best_match
        is_exact = normalize_name(acc_name)[0] == normalize_name(loc)[0]
        entry = (acc_id, acc_name, acc_county, loc, species, round(best_score, 3), county_ok)

        if is_exact:
            api_exact.append(entry)
        elif best_score >= 0.80 and county_ok:
            api_high.append(entry)
        elif best_score >= 0.65 and county_ok:
            api_medium.append(entry)
        else:
            api_unmatched.append(acc_name)

    api_auto = api_exact + api_high
    print(f"EXACT matches (auto-apply):   {len(api_exact)}")
    print(f"HIGH matches (auto-apply):    {len(api_high)}")
    print(f"MEDIUM matches (review only): {len(api_medium)}")
    print(f"Unmatched:                    {len(api_unmatched)}")
    print()

    if api_auto:
        print("=== API AUTO-APPLY ===")
        for acc_id, acc_name, acc_county, loc, species, score, county_ok in api_auto:
            flag = "EXACT" if score >= 0.99 else "HIGH"
            print(f"  [{flag} {score}] {acc_name} ({acc_county}) ← {loc} → {species}")
            if args.apply:
                cur.execute(
                    "UPDATE water_bodies SET species_primary = %s WHERE id = %s",
                    (species, acc_id),
                )
        print()

    if api_medium:
        print("=== API MEDIUM — review before applying ===")
        for acc_id, acc_name, acc_county, loc, species, score, county_ok in api_medium:
            print(f"  [{score}] {acc_name} ({acc_county}) ← {loc} → {species}")
        print()

    if api_unmatched:
        print(f"=== STILL UNMATCHED ({len(api_unmatched)}) — likely wild/unstocked fisheries ===")
        for name in api_unmatched[:30]:
            print(f"  {name}")
        if len(api_unmatched) > 30:
            print(f"  ... and {len(api_unmatched) - 30} more")
        print()

    if args.apply:
        conn.commit()
        print(f"Pass 2 applied {len(api_auto)} updates.")
    else:
        conn.rollback()
        print("Dry run (pass 2) — pass --apply to write changes.")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
