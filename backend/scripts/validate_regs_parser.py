"""
§18.8 WDFW regulations parser spot-check validation script.

Usage (run from backend/ directory):
  python -m scripts.validate_regs_parser

Output: table of fishing_regs for the five required spot-check rivers.

MUST be run before marking Phase 3 complete. Manually verify that open_dates,
gear, size_limits, and bag_limits match the published PDF regulations for each
of the five rivers: Yakima, Snoqualmie, Skykomish, Sauk, Hoh.

See §18.8 and §19.1 for what to do if the parser returns empty results
(scraper structure has likely changed — update fingerprint and parser).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conditions.wdfw_regulations import parse_wdfw_regulations, _BASE_URL
from exceptions import ScraperStructureError

# §18.8 required spot-check rivers
SPOT_CHECK_RIVERS = ["Yakima", "Snoqualmie", "Skykomish", "Sauk", "Hoh"]


def _separator(width: int = 60) -> str:
    return "-" * width


async def _run() -> None:
    print(f"\nWDFW Regulations Parser Spot-Check — Phase 3 Exit Validation")
    print(f"Source: {_BASE_URL}")
    print(_separator())

    try:
        results = await parse_wdfw_regulations(spot_check_only=SPOT_CHECK_RIVERS)
    except ScraperStructureError as exc:
        print(f"\nCRITICAL: ScraperStructureError")
        print(f"  Source: {exc.source}")
        print(f"  URL:    {exc.url}")
        print(f"  Detail: {exc.detail}")
        print(
            "\nThe page structure has changed. Update the fingerprint selector "
            "and parser in conditions/wdfw_regulations.py before re-running."
        )
        sys.exit(1)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)

    if not results:
        print("\nWARNING: Parser returned no results for the spot-check rivers.")
        print("This may mean:")
        print("  1. The page structure has changed (check the fingerprint selector)")
        print("  2. The river names don't match the water body names on the page")
        print(f"  3. The page at {_BASE_URL} is temporarily unavailable")
        print("\nVerify manually and update the parser as needed (§19.1).")
        sys.exit(1)

    found = 0
    missing = []

    for river in SPOT_CHECK_RIVERS:
        # Find matching entry (name may not be exact)
        match_key = next(
            (k for k in results if river.lower() in k.lower()),
            None,
        )

        print(f"\n{river}")
        if match_key is None:
            print(f"  NOT FOUND in parser output")
            missing.append(river)
            continue

        found += 1
        regs = results[match_key]
        print(f"  matched name:  {match_key}")
        print(f"  gear:          {regs.get('gear') or '(not parsed)'}")
        print(f"  open_dates:    {regs.get('open_dates') or '(not parsed)'}")
        print(f"  fly_legal:     {regs.get('fly_fishing_legal', True)}")
        print(f"  size_limits:   {regs.get('size_limits') or '(not parsed)'}")
        print(f"  bag_limits:    {regs.get('bag_limits') or '(not parsed)'}")
        print(f"  special_rules: {regs.get('special_rules') or '(none)'}")
        print(f"  year_round_closed: {regs.get('year_round_closed', False)}")

    print(f"\n{_separator()}")
    print(f"Results: {found}/{len(SPOT_CHECK_RIVERS)} rivers found in parser output")

    if missing:
        print(f"Missing: {', '.join(missing)}")
        print(
            "\nFor missing rivers: check if the water body name on the page differs "
            "from the common name used above. Update SPOT_CHECK_RIVERS or the parser's "
            "name matching if needed."
        )

    print(
        "\nNEXT STEPS (Phase 3 exit requirement):"
        "\n  1. Cross-reference each river's gear / open_dates / bag_limits"
        "\n     against the published WDFW PDF regulations."
        "\n  2. If values match: update the fingerprint validation date at the"
        "\n     top of conditions/wdfw_regulations.py (# Last validated: YYYY-MM-DD)."
        "\n  3. If values don't match or parser returned nothing: update the"
        "\n     parser and re-run this script (see §19.1)."
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
