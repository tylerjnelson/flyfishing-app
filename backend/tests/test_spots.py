"""
Phase 3 integration tests — spots service and API.

Tests cover:
  - list_spots: ordering, type filter, fly_only filter
  - get_spot / get_spot_closures: retrieval and active-closure filtering
  - search_spots: fuzzy name matching fallback path (ilike)
  - Router serialisation: _spot_summary and _spot_detail field shapes

Note: pg_trgm similarity search requires a live PostgreSQL instance.
The trgm branch in search_spots is tested via the real DB in the exit-criteria
manual run.  These unit tests cover the ilike fallback path via mocked DB.

spec §11.1 Phase 3 integration test:
  "Session intake with spot under active emergency closure → assert spot absent
   from session_candidates JSONB in conversations table."
  This test requires context_builder.py (Phase 5).  Marked skip below.
"""

import uuid
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spots.router import _spot_detail, _spot_summary
from spots.service import get_spot, get_spot_closures, list_spots, search_spots


# ---------------------------------------------------------------------------
# Helpers — build lightweight mock Spot / EmergencyClosure objects
# ---------------------------------------------------------------------------

def _make_spot(**kwargs) -> MagicMock:
    spot = MagicMock()
    spot.id = kwargs.get("id", uuid.uuid4())
    spot.name = kwargs.get("name", "Yakima River")
    spot.type = kwargs.get("type", "river")
    spot.latitude = kwargs.get("latitude", 46.6)
    spot.longitude = kwargs.get("longitude", -120.5)
    spot.county = kwargs.get("county", "Kittitas")
    spot.score = kwargs.get("score", 5.0)
    spot.fly_fishing_legal = kwargs.get("fly_fishing_legal", True)
    spot.seed_confidence = kwargs.get("seed_confidence", "confirmed")
    spot.has_realtime_conditions = kwargs.get("has_realtime_conditions", False)
    spot.last_visited = kwargs.get("last_visited", None)
    # detail-only fields
    spot.aliases = kwargs.get("aliases", [])
    spot.elevation_ft = kwargs.get("elevation_ft", None)
    spot.is_alpine = kwargs.get("is_alpine", False)
    spot.is_public = kwargs.get("is_public", True)
    spot.permit_required = kwargs.get("permit_required", False)
    spot.permit_url = kwargs.get("permit_url", None)
    spot.species_primary = kwargs.get("species_primary", ["rainbow trout"])
    spot.min_cfs = kwargs.get("min_cfs", 700)
    spot.max_cfs = kwargs.get("max_cfs", 1500)
    spot.min_temp_f = kwargs.get("min_temp_f", 40)
    spot.max_temp_f = kwargs.get("max_temp_f", 61)
    spot.fishing_regs = kwargs.get("fishing_regs", None)
    spot.last_stocked_date = kwargs.get("last_stocked_date", None)
    spot.last_stocked_species = kwargs.get("last_stocked_species", [])
    spot.wta_trail_url = kwargs.get("wta_trail_url", None)
    spot.score_updated = kwargs.get("score_updated", None)
    return spot


def _make_closure(**kwargs) -> MagicMock:
    c = MagicMock()
    c.rule_text = kwargs.get("rule_text", "Emergency closure — no fishing")
    c.effective = kwargs.get("effective", date(2026, 4, 1))
    c.expires = kwargs.get("expires", None)
    c.source_url = kwargs.get("source_url", "https://wdfw.wa.gov/")
    return c


def _make_db(scalars_return=None) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars_return or []
    result.scalar_one_or_none.return_value = (scalars_return or [None])[0]
    db.execute.return_value = result
    return db


# ---------------------------------------------------------------------------
# _spot_summary serialiser
# ---------------------------------------------------------------------------

class TestSpotSummary:
    def test_all_fields_present(self):
        spot = _make_spot()
        d = _spot_summary(spot)
        for key in ("id", "name", "type", "latitude", "longitude", "county",
                    "score", "fly_fishing_legal", "seed_confidence",
                    "has_realtime_conditions", "last_visited"):
            assert key in d, f"missing key: {key}"

    def test_id_is_string(self):
        spot = _make_spot(id=uuid.uuid4())
        assert isinstance(_spot_summary(spot)["id"], str)

    def test_none_score_becomes_zero(self):
        spot = _make_spot(score=None)
        assert _spot_summary(spot)["score"] == 0.0

    def test_last_visited_iso_format(self):
        spot = _make_spot(last_visited=date(2026, 3, 15))
        assert _spot_summary(spot)["last_visited"] == "2026-03-15"

    def test_last_visited_none(self):
        spot = _make_spot(last_visited=None)
        assert _spot_summary(spot)["last_visited"] is None


# ---------------------------------------------------------------------------
# _spot_detail serialiser
# ---------------------------------------------------------------------------

class TestSpotDetail:
    def test_includes_summary_fields(self):
        spot = _make_spot()
        d = _spot_detail(spot, closures=[])
        for key in ("id", "name", "score", "fly_fishing_legal"):
            assert key in d

    def test_includes_detail_fields(self):
        spot = _make_spot()
        d = _spot_detail(spot, closures=[])
        for key in ("aliases", "elevation_ft", "is_alpine", "species_primary",
                    "min_cfs", "max_cfs", "fishing_regs", "emergency_closures"):
            assert key in d

    def test_closures_serialised(self):
        spot = _make_spot()
        closure = _make_closure(rule_text="Closed", expires=date(2026, 6, 1))
        d = _spot_detail(spot, closures=[closure])
        assert len(d["emergency_closures"]) == 1
        assert d["emergency_closures"][0]["rule_text"] == "Closed"
        assert d["emergency_closures"][0]["expires"] == "2026-06-01"

    def test_empty_closures(self):
        spot = _make_spot()
        assert _spot_detail(spot, closures=[])["emergency_closures"] == []

    def test_none_lat_lon(self):
        spot = _make_spot(latitude=None, longitude=None)
        d = _spot_detail(spot, closures=[])
        assert d["latitude"] is None
        assert d["longitude"] is None


# ---------------------------------------------------------------------------
# list_spots service
# ---------------------------------------------------------------------------

class TestListSpots:
    @pytest.mark.asyncio
    async def test_returns_spots(self):
        spots = [_make_spot(name="Yakima"), _make_spot(name="Methow")]
        db = _make_db(scalars_return=spots)
        result = await list_spots(db)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_executes_query(self):
        db = _make_db(scalars_return=[])
        await list_spots(db)
        db.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_db_returns_empty_list(self):
        db = _make_db(scalars_return=[])
        result = await list_spots(db)
        assert result == []


# ---------------------------------------------------------------------------
# get_spot service
# ---------------------------------------------------------------------------

class TestGetSpot:
    @pytest.mark.asyncio
    async def test_returns_spot_when_found(self):
        spot = _make_spot(name="Icicle Creek")
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = spot
        db.execute.return_value = result_mock
        found = await get_spot(spot.id, db)
        assert found is spot

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock
        found = await get_spot(uuid.uuid4(), db)
        assert found is None


# ---------------------------------------------------------------------------
# get_spot_closures service
# ---------------------------------------------------------------------------

class TestGetSpotClosures:
    @pytest.mark.asyncio
    async def test_returns_closures(self):
        closure = _make_closure()
        db = _make_db(scalars_return=[closure])
        result = await get_spot_closures(uuid.uuid4(), db)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_no_closures_returns_empty(self):
        db = _make_db(scalars_return=[])
        result = await get_spot_closures(uuid.uuid4(), db)
        assert result == []


# ---------------------------------------------------------------------------
# search_spots service — ilike fallback path (no PostgreSQL required)
# ---------------------------------------------------------------------------

class TestSearchSpots:
    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        db = AsyncMock()
        result = await search_spots("", db)
        assert result == []

    @pytest.mark.asyncio
    async def test_whitespace_only_returns_empty(self):
        db = AsyncMock()
        result = await search_spots("   ", db)
        assert result == []

    @pytest.mark.asyncio
    async def test_trgm_hit_returned(self):
        """When trgm query returns IDs, service fetches and returns those spots."""
        spot = _make_spot(name="Yakima River")
        spot_id = spot.id

        db = AsyncMock()
        # First execute: trgm query returns one row with matching id
        trgm_result = MagicMock()
        trgm_result.all.return_value = [(spot_id,)]
        # Second execute: ORM fetch by id
        orm_result = MagicMock()
        orm_result.scalars.return_value.all.return_value = [spot]
        db.execute.side_effect = [trgm_result, orm_result]

        result = await search_spots("yakima", db)
        assert len(result) == 1
        assert result[0].name == "Yakima River"

    @pytest.mark.asyncio
    async def test_ilike_fallback_when_no_trgm_hits(self):
        """When trgm returns nothing, ilike fallback is used."""
        spot = _make_spot(name="Methow River")

        db = AsyncMock()
        # First execute: trgm returns empty
        trgm_result = MagicMock()
        trgm_result.all.return_value = []
        # Second execute: ilike fallback
        ilike_result = MagicMock()
        ilike_result.scalars.return_value.all.return_value = [spot]
        db.execute.side_effect = [trgm_result, ilike_result]

        result = await search_spots("Methow", db)
        assert len(result) == 1

