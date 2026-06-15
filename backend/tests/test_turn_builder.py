"""
Turn builder unit tests — Phase 2.

parse_recommend_block tests are pure/synchronous.
build_turn tests use AsyncMock to avoid a real DB connection.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.turn_builder import build_turn, parse_recommend_block

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

UUID_A = str(uuid.uuid4())
UUID_B = str(uuid.uuid4())
UUID_C = str(uuid.uuid4())

VALID_BLOCK = f"[RECOMMEND: {UUID_A}, {UUID_B}, {UUID_C}]"

CANDIDATE_A = {
    "spot_id": UUID_A,
    "spot_name": "Pilchuck River",
    "water_body_name": "Pilchuck River",
    "spot_type": "river",
    "drive_minutes": 45,
    "is_haversine": False,
    "straight_line_miles": None,
    "session_score": 8.5,
    "last_visited": "2025-08-15",
    "warnings": [],
    "conditions": {
        "usgs": {"cfs": 234, "trend": "dropping", "temp_f": 52.3},
        "noaa_nws": {"current": {"short_forecast": "Partly Cloudy", "temp_f": 58.0}},
        "airnow": {"aqi": 12},
    },
}

CANDIDATE_B = {
    "spot_id": UUID_B,
    "spot_name": "Skykomish River",
    "water_body_name": "Skykomish River",
    "spot_type": "river",
    "drive_minutes": 55,
    "is_haversine": False,
    "straight_line_miles": None,
    "session_score": 7.2,
    "last_visited": None,
    "warnings": ["High flow advisory"],
    "conditions": {
        "usgs": {"cfs": 1200, "trend": "stable", "temp_f": None},
        "noaa_nws": {"current": {"short_forecast": "Cloudy", "temp_f": 54.0}},
        "airnow": None,
    },
}

CANDIDATE_C = {
    "spot_id": UUID_C,
    "spot_name": None,
    "water_body_name": "Snoqualmie River",
    "spot_type": "river",
    "drive_minutes": None,
    "is_haversine": True,
    "straight_line_miles": 28.5,
    "session_score": 6.1,
    "last_visited": None,
    "warnings": [],
    "conditions": {},
}

ALL_CANDIDATES = [CANDIDATE_A, CANDIDATE_B, CANDIDATE_C]


def make_db_mock(spot_rows=None, note_rows=None):
    """
    Return an AsyncMock DB session whose execute() yields canned rows.
    First call returns fishing spot + water body rows; second call returns note counts.
    """
    db = AsyncMock()

    spot_rows = spot_rows or []
    note_rows = note_rows or []

    call_count = 0

    async def fake_execute(stmt):
        nonlocal call_count
        call_count += 1
        result = MagicMock()
        if call_count == 1:
            result.__iter__ = MagicMock(return_value=iter(spot_rows))
        else:
            result.__iter__ = MagicMock(return_value=iter(note_rows))
        return result

    db.execute = fake_execute
    return db


def make_fs_row(spot_id: str):
    """Build a stub row returned by the FishingSpot+WaterBody join query."""
    fs = MagicMock()
    fs.id = uuid.UUID(spot_id)

    wb = MagicMock()
    wb.fly_fishing_legal = True
    wb.fishing_regs = {"method": "fly only"}
    wb.last_stocked_date = None
    wb.last_stocked_species = []
    wb.species_primary = ["cutthroat", "rainbow"]

    row = MagicMock()
    row.FishingSpot = fs
    row.WaterBody = wb
    return row


def make_note_row(spot_id: str, count: int):
    row = MagicMock()
    row.fishing_spot_id = uuid.UUID(spot_id)
    row.cnt = count
    return row


# ---------------------------------------------------------------------------
# parse_recommend_block — pure / synchronous
# ---------------------------------------------------------------------------

class TestParseRecommendBlock:
    def test_extracts_three_uuids(self):
        narrative, uuids = parse_recommend_block(
            f"Here are my picks.\n\n{VALID_BLOCK}"
        )
        assert uuids == [UUID_A, UUID_B, UUID_C]

    def test_strips_block_from_narrative(self):
        text = f"Great conditions today.\n\n{VALID_BLOCK}"
        narrative, _ = parse_recommend_block(text)
        assert "[RECOMMEND" not in narrative
        assert "Great conditions today" in narrative

    def test_no_block_returns_empty_uuids(self):
        narrative, uuids = parse_recommend_block("Just some narrative text.")
        assert uuids == []
        assert narrative == "Just some narrative text."

    def test_extra_spaces_handled(self):
        block = f"[RECOMMEND:  {UUID_A} , {UUID_B} , {UUID_C} ]"
        _, uuids = parse_recommend_block(block)
        assert uuids == [UUID_A, UUID_B, UUID_C]

    def test_case_insensitive(self):
        block = f"[recommend: {UUID_A}, {UUID_B}, {UUID_C}]"
        _, uuids = parse_recommend_block(block)
        assert len(uuids) == 3

    def test_narrative_stripped_of_whitespace(self):
        _, uuids = parse_recommend_block(f"  text  \n{VALID_BLOCK}")
        assert len(uuids) == 3


# ---------------------------------------------------------------------------
# build_turn — async, DB-mocked
# ---------------------------------------------------------------------------

class TestBuildTurn:
    @pytest.mark.asyncio
    async def test_success_returns_three_cards(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        note_rows = [make_note_row(UUID_A, 3)]
        db = make_db_mock(spot_rows, note_rows)

        result = await build_turn(
            narrative="Great conditions today.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        assert "error" not in result
        assert result["narrative"] == "Great conditions today."
        assert len(result["cards"]) == 3

    @pytest.mark.asyncio
    async def test_card_order_matches_recommend_block(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        db = make_db_mock(spot_rows)

        result = await build_turn(
            narrative="Picks for today.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        ids = [c["spot_id"] for c in result["cards"]]
        assert ids == [UUID_A, UUID_B, UUID_C]

    @pytest.mark.asyncio
    async def test_card_fields_from_candidate(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        db = make_db_mock(spot_rows)

        result = await build_turn(
            narrative="Narrative.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        card_a = next(c for c in result["cards"] if c["spot_id"] == UUID_A)
        assert card_a["name"] == "Pilchuck River"
        assert card_a["drive_minutes"] == 45
        assert card_a["conditions"]["cfs"] == 234
        assert card_a["conditions"]["cfs_trend"] == "dropping"
        assert card_a["conditions"]["water_temp_f"] == 52.3
        assert card_a["conditions"]["weather_summary"] == "Partly Cloudy"
        assert card_a["conditions"]["air_temp_f"] == 58.0
        assert card_a["conditions"]["aqi"] == 12

    @pytest.mark.asyncio
    async def test_card_name_falls_back_to_water_body_name(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        db = make_db_mock(spot_rows)

        result = await build_turn(
            narrative="N.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        card_c = next(c for c in result["cards"] if c["spot_id"] == UUID_C)
        assert card_c["name"] == "Snoqualmie River"

    @pytest.mark.asyncio
    async def test_db_fields_populated(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        note_rows = [make_note_row(UUID_A, 5)]
        db = make_db_mock(spot_rows, note_rows)

        result = await build_turn(
            narrative="N.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        card_a = next(c for c in result["cards"] if c["spot_id"] == UUID_A)
        assert card_a["fly_fishing_legal"] is True
        assert card_a["fishing_regs"] == {"method": "fly only"}
        assert card_a["species_primary"] == ["cutthroat", "rainbow"]
        assert card_a["note_count"] == 5

    @pytest.mark.asyncio
    async def test_note_count_zero_when_none(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        db = make_db_mock(spot_rows, note_rows=[])

        result = await build_turn(
            narrative="N.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        for card in result["cards"]:
            assert card["note_count"] == 0

    @pytest.mark.asyncio
    async def test_missing_recommend_block_returns_error(self):
        db = make_db_mock()

        result = await build_turn(
            narrative="Narrative only.",
            recommend_block=None,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        assert result["error"] == "missing_recommend_block"
        assert result["cards"] == []
        assert result["narrative"] == "Narrative only."

    @pytest.mark.asyncio
    async def test_wrong_uuid_count_returns_error(self):
        db = make_db_mock()
        bad_block = f"[RECOMMEND: {UUID_A}, {UUID_B}]"  # only 2

        result = await build_turn(
            narrative="N.",
            recommend_block=bad_block,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        assert result["error"] == "invalid_recommend_block"
        assert result["cards"] == []

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_error(self):
        db = make_db_mock()
        bad_block = f"[RECOMMEND: {UUID_A}, not-a-uuid, {UUID_C}]"

        result = await build_turn(
            narrative="N.",
            recommend_block=bad_block,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        assert result["error"] == "invalid_recommend_block"

    @pytest.mark.asyncio
    async def test_candidate_not_in_session_returns_empty_fields(self):
        unknown_id = str(uuid.uuid4())
        block = f"[RECOMMEND: {UUID_A}, {UUID_B}, {unknown_id}]"
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, unknown_id]]
        db = make_db_mock(spot_rows)

        result = await build_turn(
            narrative="N.",
            recommend_block=block,
            candidates=[CANDIDATE_A, CANDIDATE_B],  # unknown_id not in candidates
            db=db,
        )

        unknown_card = next(c for c in result["cards"] if c["spot_id"] == unknown_id)
        assert unknown_card["name"] == ""
        assert unknown_card["drive_minutes"] is None
        assert unknown_card["conditions"]["cfs"] is None

    @pytest.mark.asyncio
    async def test_warnings_list_is_copy(self):
        spot_rows = [make_fs_row(u) for u in [UUID_A, UUID_B, UUID_C]]
        db = make_db_mock(spot_rows)

        result = await build_turn(
            narrative="N.",
            recommend_block=VALID_BLOCK,
            candidates=ALL_CANDIDATES,
            db=db,
        )

        card_b = next(c for c in result["cards"] if c["spot_id"] == UUID_B)
        assert card_b["warnings"] == ["High flow advisory"]
