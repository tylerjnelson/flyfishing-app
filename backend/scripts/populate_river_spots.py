"""
Populate water_body coordinates, CFS thresholds, and fishing_spots for USGS-monitored rivers.

Covers 16 rivers that have USGS site IDs but no fishing_spots (or incomplete data):
  - Adds/corrects lat/lon on the water_body (representative point)
  - Sets min_cfs / max_cfs fly-fishing thresholds
  - Inserts one named fishing_spot (primary public access) per canonical river
  - Adds USGS site IDs + has_realtime_conditions for Sauk and Skykomish

WDFW stocking-carrier duplicates (KALAMA R 27.0002, SOL DUC R 20.0096) are
intentionally skipped — they will be retired in a later cleanup pass.

Dry-run by default. Pass --apply to write changes.

Usage (from backend/):
  sudo /opt/flyfish/venv/bin/python scripts/populate_river_spots.py
  sudo /opt/flyfish/venv/bin/python scripts/populate_river_spots.py --apply
"""

import argparse
import os
import sys

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# River data
# ---------------------------------------------------------------------------
# Each entry:
#   name         — must match water_bodies.name exactly
#   lat/lon      — representative point (also used for fishing_spot coords)
#   min_cfs      — lower CFS hard filter (fly fishing viable floor)
#   max_cfs      — upper CFS hard filter (blown out ceiling)
#   spot_name    — named fishing_spot (primary public access)
#   county       — set on water_body if currently null
#   usgs_ids     — set usgs_site_ids if not already populated
#   has_realtime — set has_realtime_conditions=True if not already

RIVERS = [
    {
        "name": "Green River",
        "lat": 47.2842, "lon": -121.9865,
        "min_cfs": 300, "max_cfs": 1800,
        "spot_name": "Flaming Geyser State Park",
        "county": "King",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Kalama River",
        "lat": 46.0475, "lon": -122.8367,
        "min_cfs": 300, "max_cfs": 1500,
        "spot_name": "Modrow Bridge",
        "county": "Cowlitz",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Klickitat River",
        "lat": 45.9377, "lon": -121.1191,
        "min_cfs": 400, "max_cfs": 2500,
        "spot_name": "Leidl Campground",
        "county": "Klickitat",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Little Wenatchee River",
        "lat": 47.8269, "lon": -120.8725,
        "min_cfs": 80, "max_cfs": 500,
        "spot_name": "Little Wenatchee Ford Campground",
        "county": "Chelan",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Methow River",
        "lat": 48.4716, "lon": -120.1773,
        "min_cfs": 200, "max_cfs": 2500,
        "spot_name": "Winthrop Town Reach",
        "county": "Okanogan",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Naches River",
        "lat": 46.7282, "lon": -120.7098,
        "min_cfs": 150, "max_cfs": 1000,
        "spot_name": "Naches WDFW Access",
        "county": "Yakima",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "North Fork Stillaguamish",
        "lat": 48.0990, "lon": -121.9783,
        "min_cfs": 200, "max_cfs": 1200,
        "spot_name": "Jordan Road Wade Access",
        "county": "Snohomish",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Pilchuck River",
        "lat": 47.9872, "lon": -122.0353,
        "min_cfs": 80, "max_cfs": 600,
        "spot_name": "Old Monroe Road Access",
        "county": "Snohomish",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Queets River",
        "lat": 47.6260, "lon": -124.0178,
        "min_cfs": 800, "max_cfs": 6000,
        "spot_name": "Queets Campground",
        "county": "Jefferson",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        # WDFW stocking name — canonical Skagit entry (no separate "Skagit River" row with USGS ID)
        "name": "SKAGIT R     03.0176",
        "lat": 48.5354, "lon": -121.7490,
        "min_cfs": 2000, "max_cfs": 9000,   # already set; kept here for completeness
        "spot_name": "Concrete Area Access",
        "county": "Skagit",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Sol Duc River",
        "lat": 47.9142, "lon": -124.5409,
        "min_cfs": 300, "max_cfs": 2000,
        "spot_name": "Leyendecker Park",
        "county": "Clallam",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Stillaguamish River",
        "lat": 48.1599, "lon": -122.1235,
        "min_cfs": 400, "max_cfs": 2000,
        "spot_name": "Haller Park",
        "county": "Snohomish",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Tolt River",
        "lat": 47.6478, "lon": -121.9056,
        "min_cfs": 100, "max_cfs": 700,
        "spot_name": "Carnation Farm Road",
        "county": "King",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        "name": "Wenatchee River",
        "lat": 47.5958, "lon": -120.6614,
        "min_cfs": 400, "max_cfs": 3000,
        "spot_name": "Icicle Confluence Wade Access",
        "county": "Chelan",
        "usgs_ids": None,
        "has_realtime": True,
    },
    {
        # Sauk has no USGS ID in DB yet — add it
        "name": "Sauk River",
        "lat": 48.3048, "lon": -121.5165,
        "min_cfs": 300, "max_cfs": 3000,
        "spot_name": "Sauk Prairie",
        "county": "Skagit",
        "usgs_ids": ["12189500"],
        "has_realtime": True,
    },
    {
        # Skykomish has no USGS ID in DB yet — add it
        "name": "Skykomish River",
        "lat": 47.8381, "lon": -121.6107,
        "min_cfs": 500, "max_cfs": 3000,
        "spot_name": "Gold Bar Reach",
        "county": "Snohomish",
        "usgs_ids": ["12134500"],
        "has_realtime": True,
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes to DB")
    args = parser.parse_args()

    db_url = os.environ.get("DATABASE_URL", "postgresql://<user>:<password>@localhost/flyfish")
    db_url = db_url.replace("postgresql+asyncpg://", "postgresql://")

    conn = psycopg2.connect(db_url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    wb_updated = 0
    spots_created = 0
    not_found = []

    for r in RIVERS:
        cur.execute(
            "SELECT id, name, latitude, longitude, min_cfs, max_cfs, county, "
            "usgs_site_ids, has_realtime_conditions FROM water_bodies WHERE name = %s",
            (r["name"],),
        )
        row = cur.fetchone()
        if not row:
            not_found.append(r["name"])
            print(f"  NOT FOUND: {r['name']}")
            continue

        wb_id = row["id"]

        # --- Update water_body ---
        updates = {}
        if row["latitude"] is None:
            updates["latitude"] = r["lat"]
        if row["longitude"] is None:
            updates["longitude"] = r["lon"]
        if row["min_cfs"] is None:
            updates["min_cfs"] = r["min_cfs"]
        if row["max_cfs"] is None:
            updates["max_cfs"] = r["max_cfs"]
        if row["county"] is None and r["county"]:
            updates["county"] = r["county"]
        if r["usgs_ids"] and (not row["usgs_site_ids"] or row["usgs_site_ids"] == []):
            updates["usgs_site_ids"] = r["usgs_ids"]
        if r["has_realtime"] and not row["has_realtime_conditions"]:
            updates["has_realtime_conditions"] = True

        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates)
            values = list(updates.values()) + [wb_id]
            change_summary = ", ".join(
                f"{k}={v}" for k, v in updates.items()
            )
            print(f"  UPDATE water_bodies [{r['name']}]: {change_summary}")
            if args.apply:
                cur.execute(
                    f"UPDATE water_bodies SET {set_clause} WHERE id = %s",
                    values,
                )
            wb_updated += 1
        else:
            print(f"  SKIP water_bodies [{r['name']}]: already has all fields")

        # --- Check / insert fishing_spot ---
        cur.execute(
            "SELECT id, name FROM fishing_spots WHERE water_body_id = %s",
            (wb_id,),
        )
        existing_spots = cur.fetchall()

        if existing_spots:
            names = [s["name"] for s in existing_spots]
            print(f"  SKIP fishing_spots [{r['name']}]: already has spots {names}")
            continue

        print(
            f"  INSERT fishing_spot [{r['name']}]: '{r['spot_name']}' "
            f"({r['lat']}, {r['lon']})"
        )
        if args.apply:
            cur.execute(
                """
                INSERT INTO fishing_spots
                    (id, water_body_id, name, latitude, longitude, is_public, permit_required, created_at)
                VALUES
                    (gen_random_uuid(), %s, %s, %s, %s, true, false, now())
                """,
                (wb_id, r["spot_name"], r["lat"], r["lon"]),
            )
        spots_created += 1

    print()
    print(f"water_bodies to update:  {wb_updated}")
    print(f"fishing_spots to create: {spots_created}")
    if not_found:
        print(f"NOT FOUND ({len(not_found)}): {not_found}")
    print()

    if args.apply:
        conn.commit()
        print("Changes applied.")
    else:
        conn.rollback()
        print("Dry run — pass --apply to write changes.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
