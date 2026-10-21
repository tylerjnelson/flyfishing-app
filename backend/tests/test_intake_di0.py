"""
DATA-INTAKE DI-0: species vocabulary, pamphlet gear rules in the spot details,
the conditions_cache read limit and nightly prune, the last-stocked recompute,
and the notes resolver only linking real fishing spots.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import chat.context_builder as cb
from conditions import cache
from conditions import wdfw_seasons as ws
from db.models import ConditionsCache
from spots import species


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --- species vocabulary ---------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Rainbow", {"trout"}),
    ("Rainbow Trout", {"trout"}),
    ("Brown Trout", {"trout"}),
    ("Eastern Brook Trout", {"trout"}),
    ("Tiger Trout", {"trout"}),
    ("Golden Trout", {"trout"}),
    ("Cutthroat", {"trout", "cutthroat"}),
    ("Lahontan Cutthroat Trout", {"trout", "cutthroat"}),
    ("Steelhead", {"steelhead"}),
    ("Coho", {"salmon"}),
    ("Chinook", {"salmon"}),
    ("Chum", {"salmon"}),
    ("Sockeye", {"salmon"}),
    ("Kokanee", set()),
    ("Channel Catfish", set()),
    ("Tiger Muskie", set()),
    (None, set()),
])
def test_species_groups(name, expected):
    assert species.groups(name) == expected


def test_targets_keep_intake_values_and_drop_any():
    assert species.targets(["trout", "cutthroat"]) == {"trout", "cutthroat"}
    assert species.targets(["any"]) == set()
    assert species.targets(["trout", "any"]) == set()
    assert species.targets([]) == set()
    assert species.targets(["Rainbow"]) == {"trout"}


def _wb(primary=None, stocked=None):
    return SimpleNamespace(species_primary=primary, last_stocked_species=stocked)


def test_trout_trip_matches_a_stocked_rainbow_lake():
    # Was -0.5: {"trout"} vs {"rainbow"} as exact strings never met.
    assert cb._species_match_delta(["trout"], _wb(["Rainbow"])) == 1.5
    assert cb._species_match_delta(["trout"], _wb(None, ["Rainbow Trout"])) == 1.5


def test_species_match_partial_none_and_any():
    assert cb._species_match_delta(["trout", "steelhead"], _wb(["Rainbow", "Coho"])) == 0.5
    assert cb._species_match_delta(["steelhead"], _wb(["Rainbow"])) == -0.5
    assert cb._species_match_delta(["cutthroat"], _wb(["Rainbow"])) == -0.5
    assert cb._species_match_delta(["cutthroat"], _wb(["Cutthroat Trout"])) == 1.5
    assert cb._species_match_delta(["any"], _wb(["Rainbow"])) == 0.0
    assert cb._species_match_delta(["trout"], _wb(["Kokanee"])) == 0.0  # nothing we can group
    assert cb._species_match_delta(["trout"], _wb()) == 0.0


def test_temp_ceiling_uses_the_vocabulary():
    assert cb._species_temp_ceiling(["trout", "bass"]) == 61.0
    assert cb._species_temp_ceiling(["Rainbow"]) == 61.0
    assert cb._species_temp_ceiling(["any"]) is None


# --- pamphlet gear rules ----------------------------------------------------------

def _row(species_, dates, rules="Statewide min. size/daily limit."):
    return {"species": species_, "dates": dates, "rules": rules}


GEAR = ws.from_json({"year": "2026-27", "entries": [
    {"name": "TEST RIVER", "counties": ["KING"], "kind": "river", "reaches": [
        {"desc": "", "rows": [_row("Trout", "Sat. before Memorial Day-Oct. 31",
                                   "Fly fishing only. Catch and Release. Daily limit 0.")]}]},
    {"name": "SPLIT RIVER", "counties": ["KING"], "kind": "river", "reaches": [
        {"desc": "from mouth to Hwy. 2", "rows": [_row("Trout", "Sat. before Memorial Day-Oct. 31")]},
        {"desc": "upstream of Hwy. 2", "rows": [_row("Trout", "Sat. before Memorial Day-Oct. 31",
                                                     "Selective gear rules. Catch and Release.")]}]},
    {"name": "PLAIN LAKE", "counties": ["KING"], "kind": "lake", "reaches": [
        {"desc": "", "rows": [_row("All game fish", "Year-round")]}]},
]})


def test_gear_line_whole_water():
    line = ws.gear_line("Test River", None, "King", "river", date(2026, 7, 1), pamphlet=GEAR)
    assert line == "WDFW special rules (2026-27 pamphlet): Fly fishing only. Catch and Release."


def test_gear_line_by_stretch():
    line = ws.gear_line("Split River", None, "King", "river", date(2026, 7, 1), pamphlet=GEAR)
    assert line.startswith("WDFW special rules (2026-27 pamphlet) by stretch:")
    assert "from mouth to Hwy. 2: statewide rules" in line
    assert "upstream of Hwy. 2: Selective gear rules. Catch and Release." in line


def test_gear_line_empty_when_nothing_special_or_unknown():
    assert ws.gear_line("Plain Lake", None, "King", "lake", date(2026, 7, 1), pamphlet=GEAR) == ""
    assert ws.gear_line("Nowhere Creek", None, "King", "river", date(2026, 7, 1), pamphlet=GEAR) == ""
    assert ws.gear_line("Test River", None, "King", "river", date(2026, 12, 1), pamphlet=GEAR) == ""  # closed
    assert ws.gear_line("Test River", None, "King", "river", date(2027, 7, 1), pamphlet=GEAR) == ""   # next year


def test_shipped_pamphlet_knows_a_fly_only_stretch():
    # NF Stillaguamish, mouth to Hwy. 530: fly fishing only (TODO-22 hand check).
    line = ws.gear_line("North Fork Stillaguamish River", None, "Snohomish", "river", date(2026, 10, 20))
    assert "Fly fishing only." in line


async def test_spot_details_carry_gear_rules_not_scraped_regs():
    fs = SimpleNamespace(permit_required=False, permit_notes=None)
    wb = SimpleNamespace(name="Test River", aliases=None, county="King", type="river", fishing_regs={"x": 1},
                         fly_fishing_legal=True, species_primary=["Rainbow"], last_stocked_date=None,
                         last_stocked_species=None)
    result = MagicMock()
    result.one_or_none.return_value = (fs, wb)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    dep = datetime(2026, 7, 1, 15, tzinfo=timezone.utc)
    with patch.object(ws, "load", return_value=GEAR):
        details = await cb._fetch_spot_details(db, str(uuid.uuid4()), dep)
    assert details["gear_rules"].endswith("Fly fishing only. Catch and Release.")
    assert "fishing_regs" not in details


# --- conditions_cache read limit and prune -----------------------------------------

def test_fresh_bounds_each_per_water_source():
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    sql = _sql(select(ConditionsCache.id).where(cache.fresh(now)))
    for src, hours in cache.MAX_AGE_HOURS.items():
        assert f"'{src}'" in sql
        assert (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S") in sql
    assert "NOT IN" in sql  # global sources (inciweb, nps_alerts) are not age-limited


def test_read_limits_cover_the_realtime_sources():
    for src in ("usgs", "noaa_nws", "airnow", "noaa_nwrfc", "snotel", "wta"):
        assert src in cache.MAX_AGE_HOURS


async def test_prune_expired_deletes_only_per_water_sources():
    session = MagicMock()
    session.execute = AsyncMock(return_value=SimpleNamespace(rowcount=2))
    removed = await cache.prune_expired(session)
    sqls = [_sql(c.args[0]) for c in session.execute.await_args_list]
    assert removed == 2 * len(cache.MAX_AGE_HOURS)
    assert all(s.startswith("DELETE FROM conditions_cache") for s in sqls)
    assert not any("'inciweb'" in s or "'nps_alerts'" in s for s in sqls)


async def test_get_spot_conditions_read_is_age_limited():
    from chat import tools
    from db.models import FishingSpot, WaterBody
    first = MagicMock()
    first.one_or_none.return_value = SimpleNamespace(FishingSpot=MagicMock(spec=FishingSpot),
                                                     WaterBody=MagicMock(spec=WaterBody))
    second = MagicMock()
    second.mappings.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[first, second])
    try:
        await tools._build_candidate_from_db(db, str(uuid.uuid4()))
    except Exception:
        pass
    sql = str(db.execute.await_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    assert "conditions_cache.fetched_at >=" in sql


# --- last stocked -------------------------------------------------------------------

def test_stocking_job_recomputes_date_and_species_from_events():
    from conditions import scheduler
    sql = scheduler.REFRESH_LAST_STOCKED_SQL
    assert "last_stocked_species = last.species" in sql
    assert "FROM stocking_events" in sql
    from importlib import import_module
    mig = import_module("db.migrations.versions.0021_intake_stop_the_bleeding")
    assert " ".join(mig.REFRESH_LAST_STOCKED_SQL.split()) == " ".join(sql.split())


def test_regulations_job_is_gone():
    from conditions import scheduler
    assert not hasattr(scheduler, "job_wdfw_regulations")
    assert hasattr(scheduler, "job_cache_prune")


# --- notes resolver -------------------------------------------------------------------

def _hit(name, spot, score):
    return {"water_body_id": str(uuid.uuid4()), "spot_id": spot, "name": name, "county": None,
            "seed_confidence": "confirmed", "sem_score": score, "trgm_score": score}


async def _resolve(hits):
    from notes import spot_resolver as sr
    with patch.object(sr, "extract_location", AsyncMock(return_value={"location_string": "x", "confidence": "high"})), \
         patch.object(sr, "embed_text", AsyncMock(return_value=[0.0])), \
         patch.object(sr, "_semantic_lookup", AsyncMock(return_value=hits)), \
         patch.object(sr, "_fuzzy_lookup", AsyncMock(return_value=hits)):
        return await sr.resolve_spot("note", MagicMock())


async def test_resolver_never_links_a_water_without_a_spot():
    spot = str(uuid.uuid4())
    res = await _resolve([_hit("Ahtanum Creek", None, 1.0), _hit("South Fork Ahtanum Creek", spot, 0.9)])
    # The spotless water is the best match: no auto-link to the weaker one; offer only real spots.
    assert res["band"] == "medium" and res["auto_spot_id"] is None
    assert [c["spot_id"] for c in res["candidates"]] == [spot]


async def test_resolver_low_when_only_spotless_waters_match():
    res = await _resolve([_hit("Howell Lake", None, 1.0)])
    assert res["band"] == "low" and res["candidates"] == []


async def test_resolver_auto_links_a_spot():
    spot = str(uuid.uuid4())
    res = await _resolve([_hit("Yakima River", spot, 1.0)])
    assert res["band"] == "auto" and res["auto_spot_id"] == spot
