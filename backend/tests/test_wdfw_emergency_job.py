"""
TODO-15: job_wdfw_emergency matching and supersession.

Rules link to a water body via their title or the head of their Location
field (never boundary landmarks), and a closure is superseded by a later
reopening rule for the same water.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from conditions.scheduler import _apply_supersession

BOGACHIEL = uuid.uuid4()
MINERAL = uuid.uuid4()
STILLY = uuid.uuid4()
FISH_LAKE = uuid.uuid4()
SPEARFISH = uuid.uuid4()

_WATER_BODIES = [
    SimpleNamespace(id=BOGACHIEL, name="Bogachiel River", aliases=["Bogachiel River"]),
    SimpleNamespace(id=MINERAL, name="Mineral Lake", aliases=None),
    SimpleNamespace(id=uuid.uuid4(), name="MINERAL LK (LEWI)", aliases=None),
    SimpleNamespace(id=STILLY, name="Stillaguamish River", aliases=None),
    SimpleNamespace(id=FISH_LAKE, name="Fish Lake", aliases=None),
    SimpleNamespace(id=SPEARFISH, name="Spearfish Lake", aliases=None),
]


def _rule(title, action=None, location=None, closes=False, effective="2026-10-01", expires=None):
    return {
        "rule_text": title, "title": title, "action": action, "location": location,
        "closes_fishing": closes, "effective": effective, "expires": expires,
        "source_url": "https://wdfw.wa.gov/x", "fetched_at": "2026-10-11T00:00:00+00:00",
    }


QUILLAYUTE_LOC = ("Quillayute River (Clallam County), from Olympic National Park boundary upstream "
                  "to confluence of Sol Duc and Bogachiel rivers.")


async def _run_job(rules):
    import conditions.scheduler as sched

    added = []
    session = MagicMock()
    session.add = added.append
    lookup = MagicMock()
    lookup.all.return_value = _WATER_BODIES
    session.execute = AsyncMock(return_value=lookup)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)
    with patch.object(sched, "fetch_wdfw_emergency", AsyncMock(return_value=rules)), \
         patch.object(sched, "AsyncSessionLocal", MagicMock(return_value=ctx)):
        await sched.job_wdfw_emergency()
    return {row.title: row for row in added}


async def test_landmark_does_not_link_rule_to_bogachiel():
    rows = await _run_job([
        _rule("Quillayute River closing to all fishing due to low flows", "Closes fishing.",
              QUILLAYUTE_LOC, closes=True, effective="2026-09-09"),
    ])
    (row,) = rows.values()
    assert row.water_body_id is None  # was Bogachiel River before TODO-15
    assert row.closes_fishing is True


async def test_quillayute_reopen_supersedes_closure_without_water_body():
    rows = await _run_job([
        _rule("Quillayute River closing to all fishing due to low flows", "Closes fishing.",
              QUILLAYUTE_LOC, closes=True, effective="2026-09-09"),
        _rule("Quillayute River reopens to fishing with reduced retention limits and no bait",
              "Reopens fishing in the Quillayute River with reduced salmon retention limits.",
              QUILLAYUTE_LOC, effective="2026-10-06"),
    ])
    assert rows["Quillayute River closing to all fishing due to low flows"].closes_fishing is False


async def test_title_match_and_stocking_style_names_ignored():
    rows = await _run_job([
        _rule("Mineral Lake fishing season extended through Oct. 31",
              "Opens the Mineral Lake game fish season through Oct. 31 annually.",
              "Mineral Lake (Lewis County).", effective="2026-10-01", expires="2026-10-31"),
    ])
    (row,) = rows.values()
    assert row.water_body_id == MINERAL
    assert row.closes_fishing is False
    assert row.action.startswith("Opens")


async def test_location_head_match_when_title_has_no_water_name():
    rows = await _run_job([
        _rule("Fishing closure update", "Closes all fishing.",
              "Stillaguamish River from Marine Drive upstream", closes=True),
    ])
    assert next(iter(rows.values())).water_body_id == STILLY


async def test_whole_word_match_fish_lake_not_inside_spearfish():
    rows = await _run_job([_rule("Spearfish Lake daily limit update", "Changes daily limits.")])
    assert next(iter(rows.values())).water_body_id == SPEARFISH


def test_supersession_requires_later_reopen_on_same_water():
    closure = _rule("A closed", "Closes all fishing.", "A River", closes=True, effective="2026-10-05")
    earlier_reopen = _rule("A reopens", "Reopens fishing.", "A River", effective="2026-10-01")
    other_water = _rule("B reopens", "Reopens fishing.", "B River", effective="2026-10-09")
    gear_change = _rule("A gear", "Prohibits bait.", "A River", effective="2026-10-09")
    for r in (closure, earlier_reopen, other_water, gear_change):
        r["water_body_id"] = None
    assert _apply_supersession([closure, earlier_reopen, other_water, gear_change]) == []
    assert closure["closes_fishing"] is True


async def test_spot_page_closures_query_only_real_in_effect_closures():
    """The SpotDetail 'Active Closure' banner showed every linked rule."""
    from sqlalchemy.dialects import postgresql
    from spots.service import get_spot_closures

    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    await get_spot_closures(uuid.uuid4(), db)
    sql = str(db.execute.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "closes_fishing IS true" in sql
    assert "effective <=" in sql and "expires >=" in sql


async def test_context_builder_query_only_closes_fishing():
    import inspect
    import chat.context_builder as cb
    src = inspect.getsource(cb)
    assert "EmergencyClosure.closes_fishing.is_(True)" in src
