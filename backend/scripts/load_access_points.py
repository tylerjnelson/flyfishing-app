"""
Apply data/access_points.json (built by scripts/build_access_points.py and
reviewed) to the database (TODO-5). Needs migration 0018.

In one transaction:
  1. Merge duplicate water_bodies rows into the recommendable one ("MINERAL LK
     (LEWI)" → "Mineral Lake"): stocking events, closures and flow bands move;
     the duplicate's name becomes an alias (the stocking job matches aliases),
     its stocking date and any missing fields carry over; then it is deleted.
  2. Add the lakes the original seed dropped (one lake per name statewide).
  3. Rename stocking-style and DNR-area names ("LK DESIRE (KING)", "Yakima Basin
     watershed"), keeping the old name as an alias; retype lakes typed 'river'.
  4. Replace every access_points row with the file's.
  5. Give activated waters a fishing spot at their best access point, so they can
     be recommended.
Then re-embeds the names of new and renamed waters (Ollama; non-fatal).

Dry run by default (rolls back); --apply commits.

Usage (from backend/, with the app env):
  python -m scripts.load_access_points [--file data/access_points.json] [--apply]
"""

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

_DEFAULT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "access_points.json")

# Fields a merged duplicate fills in when the kept row has none.
_FILL = ("county", "fishing_regs", "species_primary", "usgs_site_ids", "wdfw_water_id",
         "snotel_station_id", "noaa_station_id", "wta_trail_url", "elevation_ft")

_CONF_RANK = {"high": 0, "medium": 1, "low": 2}
_SOURCE_RANK = {"wdfw_shore": 0, "wdfw_was": 0, "parks_launch": 1, "ecology": 0, "curated": 1, "osm": 2}


async def _merge(db, m: dict, counts: dict) -> None:
    src, dst = uuid.UUID(m["from_id"]), uuid.UUID(m["into_id"])
    spots = (await db.execute(text("SELECT id FROM fishing_spots WHERE water_body_id = :s"), {"s": src})).scalars().all()
    if spots and not m.get("drop_spot"):
        raise RuntimeError(f"merge source {m['from_name']} has {len(spots)} fishing spot(s); refusing")
    for spot in spots:
        # A duplicate's own spot goes only if nothing a user made points at it.
        refs = (await db.execute(text("""
            SELECT (SELECT count(*) FROM notes WHERE fishing_spot_id = :f)
                 + (SELECT count(*) FROM trips WHERE fishing_spot_id = :f)
                 + (SELECT count(*) FROM saved_spots WHERE fishing_spot_id = :f)"""), {"f": spot})).scalar()
        if refs:
            raise RuntimeError(f"merge source {m['from_name']}: its spot has {refs} note/trip/saved reference(s)")
        await db.execute(text("DELETE FROM response_cache WHERE fishing_spot_id = :f"), {"f": spot})
        await db.execute(text("DELETE FROM fishing_spots WHERE id = :f"), {"f": spot})
        counts["merge_dropped_spots"] += 1
    for table in ("stocking_events", "emergency_closures", "flow_bands"):
        r = await db.execute(text(f"UPDATE {table} SET water_body_id = :d WHERE water_body_id = :s"), {"d": dst, "s": src})
        counts[f"merge_moved_{table}"] += r.rowcount
    await db.execute(text("UPDATE water_bodies SET parent_id = :d WHERE parent_id = :s"), {"d": dst, "s": src})
    await db.execute(text("DELETE FROM conditions_cache WHERE water_body_id = :s"), {"s": src})
    fill = ", ".join(f"{f} = COALESCE(k.{f}, d.{f})" for f in _FILL)
    await db.execute(text(f"""
        UPDATE water_bodies k SET
            {fill},
            aliases = ARRAY(SELECT DISTINCT a FROM unnest(
                COALESCE(k.aliases, ARRAY[]::text[]) || ARRAY[d.name] || COALESCE(d.aliases, ARRAY[]::text[])) a
                WHERE a IS NOT NULL),
            last_stocked_species = CASE WHEN d.last_stocked_date > COALESCE(k.last_stocked_date, DATE '1900-01-01')
                                        THEN d.last_stocked_species ELSE k.last_stocked_species END,
            last_stocked_date = GREATEST(k.last_stocked_date, d.last_stocked_date),
            has_realtime_conditions = COALESCE(k.has_realtime_conditions, false) OR COALESCE(d.has_realtime_conditions, false),
            name_embedding = NULL
        FROM water_bodies d
        WHERE k.id = :d AND d.id = :s
    """), {"d": dst, "s": src})
    await db.execute(text("DELETE FROM water_bodies WHERE id = :s"), {"s": src})
    counts["merged"] += 1


async def _best_point(db, wb_id) -> tuple | None:
    rows = (await db.execute(text(
        "SELECT source, confidence, key, latitude, longitude FROM access_points WHERE water_body_id = :w"),
        {"w": wb_id})).all()
    if not rows:
        return None
    best = min(rows, key=lambda r: (_CONF_RANK.get(r.confidence, 3), _SOURCE_RANK.get(r.source, 3), r.key))
    return best.latitude, best.longitude


class _DryRun(Exception):
    """Raised inside the transaction so a dry run rolls everything back."""


async def load(path: str, apply: bool) -> dict:
    from collections import Counter

    from db.connection import AsyncSessionLocal

    with open(path) as f:
        data = json.load(f)
    aps = {a["key"]: a for a in data["access_points"]}
    counts: Counter = Counter()
    reembed: set = set()

    try:
        await _apply(AsyncSessionLocal, data, aps, counts, reembed, apply)
    except _DryRun:
        pass
    if apply and reembed:
        await _reembed(reembed, counts)
    return dict(counts)


async def _apply(AsyncSessionLocal, data, aps, counts, reembed, apply) -> None:
    async with AsyncSessionLocal() as db:
        async with db.begin():
            existing = {str(r.id): r for r in (await db.execute(text(
                "SELECT id, name, type FROM water_bodies"))).all()}
            # The file names rows by id; refuse if any id now names another water.
            for wid, e in data["water_bodies"].items():
                row = existing.get(wid)
                if row is None:
                    raise RuntimeError(f"water body {wid} ({e['name']}) not found")
                if row.name != e["name"] and row.name != e.get("rename_to"):
                    raise RuntimeError(f"water body {wid} is {row.name!r}, file says {e['name']!r}")
            for m in data["merges"]:
                if m["from_id"] not in existing:
                    counts["merge_already_done"] += 1
                    continue
                if existing[m["from_id"]].name != m["from_name"] or m["into_id"] not in existing:
                    raise RuntimeError(f"merge {m['from_name']} → {m['into_name']} doesn't match the database")

            # 1. Merges
            for m in data["merges"]:
                if m["from_id"] in existing:
                    await _merge(db, m, counts)
                    reembed.add(m["into_id"])

            # 2. New lakes (skip any added by an earlier run)
            targets: dict[str, dict] = dict(data["water_bodies"])
            for nl in data["new_water_bodies"]:
                found = (await db.execute(text(
                    "SELECT id FROM water_bodies WHERE type = 'lake' AND name = :n AND county = :c"),
                    {"n": nl["name"], "c": nl["county"]})).scalar()
                if found is None:
                    found = uuid.uuid4()
                    await db.execute(text("""
                        INSERT INTO water_bodies (id, name, type, county, aliases, source, seed_confidence,
                                                  fly_fishing_legal, min_temp_f, score)
                        VALUES (:id, :n, 'lake', :c, ARRAY[:n], 'wdfw_access', 'confirmed', true, 40, 0)
                    """), {"id": found, "n": nl["name"], "c": nl["county"]})
                    counts["new_lakes"] += 1
                    reembed.add(str(found))
                targets[str(found)] = {"name": nl["name"], "type": "lake", "county": nl["county"],
                                       "has_spot": False, "activate": True, "access": nl["access"]}

            # 3. Renames and retypes
            for wid, e in data["water_bodies"].items():
                if e.get("rename_to") and existing[wid].name != e["rename_to"]:
                    await db.execute(text("""
                        UPDATE water_bodies SET name = :new,
                            aliases = ARRAY(SELECT DISTINCT a FROM unnest(
                                COALESCE(aliases, ARRAY[]::text[]) || ARRAY[name, :new]) a),
                            name_embedding = NULL
                        WHERE id = :id
                    """), {"id": uuid.UUID(wid), "new": e["rename_to"]})
                    counts["renamed"] += 1
                    reembed.add(wid)
                if e.get("retype_to"):
                    r = await db.execute(text("UPDATE water_bodies SET type = :t WHERE id = :id AND type <> :t"),
                                         {"id": uuid.UUID(wid), "t": e["retype_to"]})
                    counts["retyped"] += r.rowcount

            # 4. Access points: the file is the whole truth.
            await db.execute(text("DELETE FROM access_points"))
            for wid, e in targets.items():
                for a in e["access"]:
                    ap = aps[a["key"]]
                    await db.execute(text("""
                        INSERT INTO access_points (water_body_id, key, source, name, kind, latitude, longitude,
                            description, parking, facilities, seasonal, pass_required, managing_entity,
                            gateway_latitude, gateway_longitude, gateway_minutes,
                            confidence, match_method, match_note)
                        VALUES (:w, :key, :source, :name, :kind, :lat, :lon, :description, :parking, :facilities,
                            :seasonal, :pass_required, :managing_entity, :gw_lat, :gw_lon, :gw_min,
                            :confidence, :method, :note)
                        ON CONFLICT (water_body_id, key) DO NOTHING
                    """), {"w": uuid.UUID(wid), "key": ap["key"], "source": ap["source"], "name": ap["name"],
                           "kind": ap["kind"], "lat": ap["lat"], "lon": ap["lon"],
                           "description": ap.get("description"), "parking": ap.get("parking"),
                           "facilities": ap.get("facilities") or None, "seasonal": ap.get("seasonal"),
                           "pass_required": ap.get("pass_required"), "managing_entity": ap.get("managing_entity"),
                           "gw_lat": ap.get("gateway_lat"), "gw_lon": ap.get("gateway_lon"),
                           "gw_min": ap.get("gateway_minutes"),
                           "confidence": a["confidence"], "method": a["method"], "note": a.get("note")})
                    counts["access_points"] += 1

            # 5. Spots for activated waters that have none.
            for wid, e in targets.items():
                if not e.get("activate"):
                    continue
                has = (await db.execute(text("SELECT count(*) FROM fishing_spots WHERE water_body_id = :w"),
                                        {"w": uuid.UUID(wid)})).scalar()
                if has:
                    continue
                pt = await _best_point(db, uuid.UUID(wid))
                if pt is None:
                    continue
                await db.execute(text("""
                    INSERT INTO fishing_spots (id, water_body_id, latitude, longitude, is_public, source)
                    VALUES (:id, :w, :lat, :lon, true, 'access_points')
                """), {"id": uuid.uuid4(), "w": uuid.UUID(wid), "lat": pt[0], "lon": pt[1]})
                counts["spots_created"] += 1

            if not apply:
                raise _DryRun()


async def _reembed(ids: set, counts) -> None:
    """name + aliases + county, as scripts/embed_spots.py did."""
    from db.connection import AsyncSessionLocal
    from rag.embedder import embed_text

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT id, name, aliases, county FROM water_bodies WHERE id = ANY(:ids) AND name_embedding IS NULL"),
            {"ids": [uuid.UUID(i) for i in ids]})).all()
        for r in rows:
            try:
                emb = await embed_text(" ".join([r.name, *(r.aliases or []), r.county or ""]).strip())
            except Exception as exc:  # Ollama down: notes just won't resolve to this water by name yet
                counts["embed_failed"] += 1
                print(f"embed failed for {r.name}: {type(exc).__name__}")
                continue
            await db.execute(text("UPDATE water_bodies SET name_embedding = CAST(:e AS vector) WHERE id = :id"),
                             {"e": str(emb), "id": r.id})
            counts["embedded"] += 1
        await db.commit()


def main():
    p = argparse.ArgumentParser(description="Apply access_points.json (dry run unless --apply)")
    p.add_argument("--file", default=_DEFAULT_FILE)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    counts = asyncio.run(load(args.file, args.apply))
    print(("APPLIED " if args.apply else "DRY RUN (rolled back) ") + json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
