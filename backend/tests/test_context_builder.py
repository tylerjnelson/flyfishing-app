"""
Context builder unit tests — Phase 5 Chunk 5.

Tests cover the pure helper functions extracted from build_context():
  - _has_active_closure     → emergency closure filter
  - _cfs_out_of_range       → CFS hard filter (only hard conditions filter)
  - _apply_variety_rotation → 60-day fresh-spot rule
  - _wildfire_near_spot     → 25 km proximity gate (used as soft penalty)

Integration tests requiring a DB are avoided here; the router-level
integration test below exercises the FILTER_UPDATE confirm-filter flow.
These tests are pure / synchronous — no DB, no Ollama, no async.

Note: Temperature, turbidity, alpine access, and AQI are now soft penalties
in _compute_volatile_delta rather than hard filters.
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.context_builder import (
    _apply_variety_rotation,
    _cfs_out_of_range,
    _has_active_closure,
    _wildfire_near_spot,
)


# ---------------------------------------------------------------------------
# Helpers — lightweight spot/closure stubs
# ---------------------------------------------------------------------------

def make_spot(**kwargs) -> MagicMock:
    """Return a MagicMock that quacks like a Spot model row."""
    defaults = dict(
        type="river",
        has_realtime_conditions=True,
        min_cfs=50,
        max_cfs=500,
        min_temp_f=34,
        is_alpine=False,
        elevation_ft=None,
        permit_required=False,
        latitude=47.5,
        longitude=-121.5,
    )
    defaults.update(kwargs)
    spot = MagicMock()
    for k, v in defaults.items():
        setattr(spot, k, v)
    return spot


def make_closure(effective=None, expires=None, rule_text="Emergency closure — Yakima River closed") -> MagicMock:
    cl = MagicMock()
    cl.effective = effective
    cl.expires = expires
    cl.rule_text = rule_text
    return cl


# ---------------------------------------------------------------------------
# _has_active_closure
# ---------------------------------------------------------------------------

class TestHasActiveClosure:
    """
    _has_active_closure(spot_name, active_closures) — text-based matching.
    active_closures is a pre-filtered list (date range already applied by caller).
    """

    def test_active_closure_matches_spot_name(self):
        cl = make_closure(rule_text="Emergency closure — Yakima River closed to fishing")
        assert _has_active_closure("Yakima River", [cl]) is True

    def test_closure_without_spot_name_does_not_match(self):
        cl = make_closure(rule_text="Emergency closure — Methow River closed to fishing")
        assert _has_active_closure("Yakima River", [cl]) is False

    def test_no_closures_returns_false(self):
        assert _has_active_closure("Yakima River", []) is False

    def test_empty_spot_name_returns_false(self):
        cl = make_closure(rule_text="Emergency closure — Yakima River closed")
        assert _has_active_closure("", [cl]) is False

    def test_closure_without_keyword_does_not_match(self):
        # rule_text mentions the spot but has no closure keyword
        cl = make_closure(rule_text="Yakima River regulation update — check local rules")
        assert _has_active_closure("Yakima River", [cl]) is False

    def test_partial_name_word_match(self):
        # "Yakima" alone is enough to match "Yakima River closed"
        cl = make_closure(rule_text="Yakima closed to all angling")
        assert _has_active_closure("Yakima River", [cl]) is True

    def test_multiple_closures_any_match_returns_true(self):
        cl1 = make_closure(rule_text="Methow River prohibited — emergency")
        cl2 = make_closure(rule_text="Icicle Creek emergency closure")
        assert _has_active_closure("Icicle Creek", [cl1, cl2]) is True

    def test_none_rule_text_does_not_raise(self):
        cl = make_closure(rule_text=None)
        assert _has_active_closure("Yakima River", [cl]) is False


# ---------------------------------------------------------------------------
# _cfs_out_of_range
# ---------------------------------------------------------------------------

class TestCfsOutOfRange:
    """
    _cfs_out_of_range returns True when CFS is outside min/max (hard filter).
    Temperature, turbidity, and other signals are now soft penalties.
    """

    def test_river_within_cfs_range_not_blocked(self):
        spot = make_spot(type="river", min_cfs=50, max_cfs=500)
        assert _cfs_out_of_range(spot, {"cfs": 200}) is False

    def test_river_below_min_cfs_blocked(self):
        spot = make_spot(type="river", min_cfs=100, max_cfs=500)
        assert _cfs_out_of_range(spot, {"cfs": 40}) is True

    def test_river_above_max_cfs_blocked(self):
        spot = make_spot(type="river", min_cfs=50, max_cfs=300)
        assert _cfs_out_of_range(spot, {"cfs": 600}) is True

    def test_high_temp_not_a_hard_filter(self):
        # Temp above species ceiling is now a soft penalty, not a hard filter
        spot = make_spot(type="river", min_cfs=None, max_cfs=None, min_temp_f=None)
        assert _cfs_out_of_range(spot, {"temp_f": 65.0}) is False

    def test_high_turbidity_not_a_hard_filter(self):
        # Turbidity is now a soft penalty
        spot = make_spot(type="river", min_cfs=None, max_cfs=None, min_temp_f=None)
        assert _cfs_out_of_range(spot, {"turbidity_fnu": 150.0}) is False

    def test_lake_never_cfs_blocked(self):
        spot = make_spot(type="lake")
        assert _cfs_out_of_range(spot, {"cfs": 0}) is False

    def test_no_realtime_data_not_blocked(self):
        spot = make_spot(type="river")
        assert _cfs_out_of_range(spot, None) is False

    def test_has_realtime_false_not_blocked(self):
        spot = make_spot(type="river", has_realtime_conditions=False)
        assert _cfs_out_of_range(spot, None) is False

    def test_cfs_none_in_data_not_blocked(self):
        spot = make_spot(type="river", min_cfs=100, max_cfs=500)
        assert _cfs_out_of_range(spot, {"cfs": None}) is False


# ---------------------------------------------------------------------------
# _wildfire_near_spot
# ---------------------------------------------------------------------------

class TestWildfireNearSpot:
    def test_fire_within_25km_blocks(self):
        fires = [{"latitude": "47.5001", "longitude": "-121.5001"}]
        assert _wildfire_near_spot(47.5, -121.5, fires) is True

    def test_fire_beyond_25km_passes(self):
        # ~111 km per degree lat → move 0.5° away ≈ 55 km
        fires = [{"latitude": "48.0", "longitude": "-121.5"}]
        assert _wildfire_near_spot(47.5, -121.5, fires) is False

    def test_no_fires_passes(self):
        # No fires in list → no wildfire nearby → function returns False (spot not blocked)
        assert _wildfire_near_spot(47.5, -121.5, []) is False

    def test_malformed_fire_entry_skipped(self):
        fires = [{"latitude": "bad", "longitude": "data"}]
        # Should not raise; just skip malformed entries
        assert _wildfire_near_spot(47.5, -121.5, fires) is False


# ---------------------------------------------------------------------------
# _apply_variety_rotation
# ---------------------------------------------------------------------------

class TestVarietyRotation:
    TODAY = date.today()

    def _make_candidate(self, spot_id: str, last_visited=None, score: float = 1.0) -> dict:
        return {
            "spot_id": spot_id,
            "spot_name": f"Spot {spot_id}",
            "session_score": score,
            "last_visited": last_visited,
        }

    def test_no_rotation_needed_when_top5_has_fresh_spot(self):
        """If one of the top-5 has last_visited=None, no rotation."""
        candidates = [
            self._make_candidate("a", last_visited=None),
            self._make_candidate("b", last_visited=self.TODAY.isoformat()),
            self._make_candidate("c", last_visited=self.TODAY.isoformat()),
            self._make_candidate("d", last_visited=self.TODAY.isoformat()),
            self._make_candidate("e", last_visited=self.TODAY.isoformat()),
        ]
        result = _apply_variety_rotation(candidates)
        assert [c["spot_id"] for c in result[:5]] == ["a", "b", "c", "d", "e"]

    def test_rotation_injects_qualifying_spot_at_position_5(self):
        """All top-5 visited recently → inject qualifying spot from pool at index 4."""
        recent = (self.TODAY - timedelta(days=10)).isoformat()
        old = (self.TODAY - timedelta(days=90)).isoformat()

        candidates = [
            self._make_candidate("a", last_visited=recent, score=10),
            self._make_candidate("b", last_visited=recent, score=9),
            self._make_candidate("c", last_visited=recent, score=8),
            self._make_candidate("d", last_visited=recent, score=7),
            self._make_candidate("e", last_visited=recent, score=6),
            self._make_candidate("fresh", last_visited=old, score=5),  # qualifies
        ]
        result = _apply_variety_rotation(candidates)
        ids = [c["spot_id"] for c in result[:5]]
        assert "fresh" in ids
        assert ids.index("fresh") == 4

    def test_rotation_spot_60_days_old_qualifies(self):
        """Exactly 60 days ago qualifies."""
        exactly_60 = (self.TODAY - timedelta(days=60)).isoformat()
        recent = (self.TODAY - timedelta(days=10)).isoformat()

        candidates = [
            self._make_candidate("a", last_visited=recent),
            self._make_candidate("b", last_visited=recent),
            self._make_candidate("c", last_visited=recent),
            self._make_candidate("d", last_visited=recent),
            self._make_candidate("e", last_visited=recent),
            self._make_candidate("old", last_visited=exactly_60),
        ]
        result = _apply_variety_rotation(candidates)
        ids = [c["spot_id"] for c in result[:5]]
        assert "old" in ids

    def test_rotation_spot_59_days_does_not_qualify(self):
        """59 days ago does not qualify — must be ≥ 60."""
        just_under = (self.TODAY - timedelta(days=59)).isoformat()
        recent = (self.TODAY - timedelta(days=10)).isoformat()

        candidates = [
            self._make_candidate("a", last_visited=recent),
            self._make_candidate("b", last_visited=recent),
            self._make_candidate("c", last_visited=recent),
            self._make_candidate("d", last_visited=recent),
            self._make_candidate("e", last_visited=recent),
            self._make_candidate("almost", last_visited=just_under),
        ]
        result = _apply_variety_rotation(candidates)
        ids = [c["spot_id"] for c in result[:5]]
        # "almost" is 59 days → doesn't qualify → no rotation
        assert "almost" not in ids

    def test_rotation_no_qualifying_spot_in_pool_unchanged(self):
        """If no pool spot qualifies, candidates are unchanged."""
        recent = (self.TODAY - timedelta(days=5)).isoformat()
        candidates = [self._make_candidate(str(i), last_visited=recent) for i in range(8)]
        result = _apply_variety_rotation(candidates)
        assert [c["spot_id"] for c in result] == [c["spot_id"] for c in candidates]

    def test_rotation_fewer_than_5_candidates_untouched(self):
        """Fewer than 5 candidates → variety rotation is a no-op (no top-5 to check)."""
        candidates = [self._make_candidate("a"), self._make_candidate("b")]
        result = _apply_variety_rotation(candidates)
        assert len(result) == 2

    def test_rotation_top5_already_has_60day_spot_no_change(self):
        """One of the top-5 visited > 60 days ago → already qualifies, no injection."""
        recent = (self.TODAY - timedelta(days=5)).isoformat()
        old = (self.TODAY - timedelta(days=70)).isoformat()
        pool_fresh = (self.TODAY - timedelta(days=90)).isoformat()

        candidates = [
            self._make_candidate("a", last_visited=recent, score=10),
            self._make_candidate("b", last_visited=recent, score=9),
            self._make_candidate("c", last_visited=old, score=8),      # qualifies
            self._make_candidate("d", last_visited=recent, score=7),
            self._make_candidate("e", last_visited=recent, score=6),
            self._make_candidate("f", last_visited=pool_fresh, score=5),
        ]
        result = _apply_variety_rotation(candidates)
        # No rotation; "f" should stay at position 5 (index 5), not injected at 4
        assert result[4]["spot_id"] == "e"
        assert result[5]["spot_id"] == "f"
