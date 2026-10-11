"""
TODO-12: conditions_cache pruning on the real-time path, get_spot's
freshest-per-source query, and the stocking job's removal of rows absent
from the current fetch.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.dialects import postgresql

from conditions.cache import CACHE_TTL_HOURS


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _session_factory(session):
    """AsyncSessionLocal() stand-in: `async with factory() as s, s.begin()`."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=None)
    begin.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin)
    return MagicMock(return_value=ctx)


async def test_write_conditions_cache_prunes_then_inserts():
    from conditions.cache import write_conditions_cache
    session = MagicMock()
    session.execute = AsyncMock()
    wb = uuid.uuid4()
    await write_conditions_cache(session, wb, "noaa_nws", {"x": 1})
    sql = _sql(session.execute.call_args[0][0])
    assert sql.startswith("DELETE FROM conditions_cache")
    assert "fetched_at <" in sql
    row = session.add.call_args[0][0]
    assert row.source == "noaa_nws" and row.water_body_id == wb and row.data_hash


def test_realtime_sources_have_ttls():
    for src in ("usgs", "noaa_nws", "airnow"):
        assert CACHE_TTL_HOURS[src] > 0


async def test_realtime_path_uses_pruning_writer():
    import chat.context_builder as cb
    wb = SimpleNamespace(id=uuid.uuid4(), name="Test River", usgs_site_ids=["12134500"])
    fs = SimpleNamespace(water_body_id=wb.id, latitude=47.8, longitude=-121.6)
    session = MagicMock()
    writer = AsyncMock()
    with patch.object(cb, "fetch_usgs_gauge", AsyncMock(return_value={"cfs": 1.0})), \
         patch.object(cb, "fetch_noaa_nws", AsyncMock(return_value={"t": 1})), \
         patch.object(cb, "fetch_airnow", AsyncMock(return_value=None)), \
         patch.object(cb, "AsyncSessionLocal", _session_factory(session)), \
         patch.object(cb, "write_conditions_cache", writer):
        await cb._fetch_and_cache_realtime([fs], [wb])
    assert sorted(c.args[2] for c in writer.await_args_list) == ["noaa_nws", "usgs"]
    session.add.assert_not_called()  # no unpruned direct inserts


async def test_get_spot_reads_freshest_row_per_source():
    from chat import tools
    from db.models import FishingSpot, WaterBody
    fs = MagicMock(spec=FishingSpot)
    wb = MagicMock(spec=WaterBody)
    first = MagicMock()
    first.one_or_none.return_value = SimpleNamespace(FishingSpot=fs, WaterBody=wb)
    second = MagicMock()
    second.mappings.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[first, second])
    try:
        await tools._build_candidate_from_db(db, str(uuid.uuid4()))
    except Exception:
        pass  # only the emitted SQL matters here
    sql = _sql(db.execute.await_args_list[1].args[0])
    assert "DISTINCT ON (conditions_cache.source)" in sql
    assert "LIMIT" not in sql


async def test_stocking_job_removes_rows_absent_from_fetch():
    import conditions.scheduler as sched
    records = [
        {"water_name": "LAKE X", "stocked_date": "2026-05-01", "species": "Rainbow",
         "count": 10, "size_description": "Legals", "source_record_id": "abc-1"},
        {"water_name": "LAKE Y", "stocked_date": "2026-05-02", "species": "Rainbow",
         "count": 20, "size_description": "Legals", "source_record_id": "def-1"},
    ]
    session = MagicMock()
    lookup = MagicMock()
    lookup.all.return_value = []
    deleted = MagicMock(rowcount=271000)
    executed = []

    async def execute(stmt, *a, **k):
        executed.append(stmt)
        sql = _sql(stmt) if hasattr(stmt, "compile") else str(stmt)
        if sql.startswith("SELECT"):
            return lookup
        if sql.startswith("DELETE"):
            return deleted
        return MagicMock(rowcount=0)

    session.execute = execute
    with patch.object(sched, "fetch_wdfw_stocking", AsyncMock(return_value=records)), \
         patch.object(sched, "AsyncSessionLocal", _session_factory(session)):
        await sched.job_wdfw_stocking()

    deletes = [s for s in executed if hasattr(s, "compile") and _sql(s).startswith("DELETE FROM stocking_events")]
    assert len(deletes) == 1
    stmt = deletes[0]
    sql = _sql(stmt)
    assert "stocked_date >=" in sql and "stocked_date <" in sql
    assert "NOT IN" in sql
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert sorted(v for k, v in params.items() if isinstance(v, list))[0] == ["abc-1", "def-1"]


async def test_stocking_job_skips_removal_when_fetch_empty():
    import conditions.scheduler as sched
    session = MagicMock()
    executed = []

    async def execute(stmt, *a, **k):
        executed.append(stmt)
        r = MagicMock()
        r.all.return_value = []
        return r

    session.execute = execute
    with patch.object(sched, "fetch_wdfw_stocking", AsyncMock(return_value=[])), \
         patch.object(sched, "AsyncSessionLocal", _session_factory(session)):
        await sched.job_wdfw_stocking()
    assert not any(hasattr(s, "compile") and _sql(s).startswith("DELETE") for s in executed)
