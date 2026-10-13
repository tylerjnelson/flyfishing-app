"""
Context builder unit tests — Phase 5 Chunk 5.

Tests cover the pure helper functions extracted from build_context():
  - _has_active_closure     → emergency closure filter
  - _candidate_flow         → flow vs. per-gauge references; flood-stage gate
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

import uuid

import pytest

from chat.context_builder import (
    _apply_variety_rotation,
    _band_sites,
    _candidate_flow,
    _compute_volatile_delta,
    _filter_excluded,
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


def make_closure(effective=None, expires=None, rule_text="Emergency closure — Yakima River closed",
                 title=None, location=None, closes_fishing=True, water_body_id=None) -> MagicMock:
    cl = MagicMock()
    cl.effective = effective
    cl.expires = expires
    cl.rule_text = rule_text
    cl.title = title if title is not None else rule_text
    cl.location = location
    cl.closes_fishing = closes_fishing
    cl.water_body_id = water_body_id  # MagicMock auto-creates truthy attrs; statewide/unmatched rows have NULL FK
    return cl


# ---------------------------------------------------------------------------
# _has_active_closure
# ---------------------------------------------------------------------------

class TestHasActiveClosure:
    """
    _has_active_closure(spot_name, active_closures, water_body_id) — only rules
    the scraper flagged closes_fishing count (TODO-15). active_closures is
    pre-filtered by date by the caller.
    """

    def test_active_closure_matches_spot_name(self):
        cl = make_closure(title="Yakima River closed to all fishing")
        assert _has_active_closure("Yakima River", [cl]) is True

    def test_closure_without_spot_name_does_not_match(self):
        cl = make_closure(title="Methow River closed to all fishing")
        assert _has_active_closure("Yakima River", [cl]) is False

    def test_no_closures_returns_false(self):
        assert _has_active_closure("Yakima River", []) is False

    def test_empty_spot_name_returns_false(self):
        cl = make_closure(title="Yakima River closed")
        assert _has_active_closure("", [cl]) is False

    def test_non_closure_rule_never_filters(self):
        # Openings, limit changes, retention closures: closes_fishing=False
        cl = make_closure(title="Lake Washington reopens for salmon fishing", closes_fishing=False)
        assert _has_active_closure("Lake Washington", [cl]) is False

    def test_linked_non_closure_never_filters(self):
        """Mineral Lake 'season extended' was linked by FK and hard-filtered the lake."""
        wb = uuid.uuid4()
        cl = make_closure(title="Mineral Lake fishing season extended through Oct. 31",
                          closes_fishing=False, water_body_id=wb)
        assert _has_active_closure("Mineral Lake", [cl], water_body_id=wb) is False

    def test_linked_closure_filters(self):
        wb = uuid.uuid4()
        cl = make_closure(title="Stillaguamish River to close to all fishing Oct. 9", water_body_id=wb)
        assert _has_active_closure("Stillaguamish River", [cl], water_body_id=wb) is True

    def test_partial_name_word_match(self):
        # "Yakima" alone is enough to match "Yakima closed..."
        cl = make_closure(title="Yakima closed to all angling")
        assert _has_active_closure("Yakima River", [cl]) is True

    def test_parenthetical_name_matches(self):
        cl = make_closure(title="Upper Green (Duwamish) River closed to fishing from Sept. 16–Oct. 31")
        assert _has_active_closure("Green River", [cl]) is True

    def test_landmark_in_location_does_not_match(self):
        """The Quillayute closure's Location ends '...confluence of Sol Duc and
        Bogachiel rivers'. That must not close the Bogachiel."""
        cl = make_closure(
            title="Quillayute River closing to all fishing due to low flows",
            rule_text="Quillayute River closing ... Location: Quillayute River (Clallam County) from the "
                      "Olympic National Park boundary upstream to confluence of Sol Duc and Bogachiel rivers.",
            location="Quillayute River (Clallam County) from the Olympic National Park boundary upstream "
                     "to confluence of Sol Duc and Bogachiel rivers.",
        )
        assert _has_active_closure("Bogachiel River", [cl]) is False
        assert _has_active_closure("Quillayute River", [cl]) is True

    def test_multiple_closures_any_match_returns_true(self):
        cl1 = make_closure(title="Methow River closed to all fishing")
        cl2 = make_closure(title="Icicle Creek closed to all fishing")
        assert _has_active_closure("Icicle Creek", [cl1, cl2]) is True

    def test_none_title_does_not_raise(self):
        cl = make_closure(rule_text=None, title="")
        assert _has_active_closure("Yakima River", [cl]) is False


# ---------------------------------------------------------------------------
# _candidate_flow — flow vs. per-gauge references (TODO-18)
# ---------------------------------------------------------------------------

_NORMALS = {5: 100.0, 10: 150.0, 25: 300.0, 50: 500.0, 75: 800.0, 90: 1500.0, 95: 2500.0}
_YAKIMA_BANDS = [
    {"gauge_site_id": "12484500", "gauge_name": "Yakima River at Umtanum", "mode": "wade",
     "min_cfs": 700, "max_cfs": 1500, "source": "Emerald Water Anglers"},
    {"gauge_site_id": "12484500", "gauge_name": "Yakima River at Umtanum", "mode": "float",
     "min_cfs": 1000, "max_cfs": 3500, "source": "Emerald Water Anglers"},
]


def _ref(bands=(), flood=None, normals=_NORMALS, site="12500450"):
    return {"site": site, "bands": list(bands), "normals": {site: normals} if normals else {},
            "flood": flood or {}}


def _bands_reading(cfs, stage=None):
    return {"gauges": {"12484500": {"cfs": cfs, "gauge_height_ft": stage}}}


class TestCandidateFlow:
    RIVER = make_spot(type="river", min_temp_f=None, is_alpine=False)

    def test_band_on_its_own_gauge_not_ours(self):
        """Union Gap at 1,800 is normal; Umtanum at 913 is in the wading range."""
        out = _candidate_flow(self.RIVER, {"cfs": 1800}, None, None, _ref(_YAKIMA_BANDS),
                              usgs_bands=_bands_reading(913))
        assert out["delta"] > 0 and out["warnings"] == []
        text = "\n".join(out["context"])
        assert "Wading 700–1,500 cfs: in range" in text
        assert "Floating 1,000–3,500 cfs: below range" in text
        assert "Yakima River at Umtanum (913 cfs)" in text

    def test_best_mode_scores_not_worst(self):
        """In the float range but above the wading range: no penalty (wade/float is the angler's call)."""
        in_both = _candidate_flow(self.RIVER, {"cfs": 1800}, None, None, _ref(_YAKIMA_BANDS),
                                  usgs_bands=_bands_reading(1200))
        float_only = _candidate_flow(self.RIVER, {"cfs": 4000}, None, None, _ref(_YAKIMA_BANDS),
                                     usgs_bands=_bands_reading(2500))
        assert float_only["delta"] == in_both["delta"] > 0
        assert "Wading 700–1,500 cfs: above range" in "\n".join(float_only["context"])

    def test_outside_every_band_penalized_high_more_than_low(self):
        low = _candidate_flow(self.RIVER, {"cfs": 400}, None, None, _ref(_YAKIMA_BANDS),
                              usgs_bands=_bands_reading(300))
        high = _candidate_flow(self.RIVER, {"cfs": 6000}, None, None, _ref(_YAKIMA_BANDS),
                               usgs_bands=_bands_reading(5000))
        assert high["delta"] < low["delta"] < 0
        assert any("Low flow: 300 cfs at Yakima River at Umtanum" in w for w in low["warnings"])
        assert any("blown out" in w for w in high["warnings"])

    def test_band_gauge_without_reading_falls_back_to_normals(self):
        out = _candidate_flow(self.RIVER, {"cfs": 2600}, None, None, _ref(_YAKIMA_BANDS))
        assert out["delta"] < 0
        assert any("Very high flow for" in w for w in out["warnings"])

    def test_normals_only_tails_score_middle_does_not(self):
        normal = _candidate_flow(self.RIVER, {"cfs": 500}, None, None, _ref())
        very_low = _candidate_flow(self.RIVER, {"cfs": 120}, None, None, _ref())
        assert normal["delta"] == 0 and normal["warnings"] == []
        assert "normal for" in normal["context"][0] and "~50th percentile" in normal["context"][0]
        assert very_low["delta"] < 0 and "Very low flow" in very_low["warnings"][0]

    def test_normals_ignored_when_reading_is_the_nwps_fallback(self):
        """A discontinued USGS gauge's record (Naches: 1909–1979) doesn't judge another gauge's reading."""
        out = _candidate_flow(self.RIVER, {"cfs": 1490, "provider": "noaa_nwps_observed"}, None, None, _ref())
        assert out["delta"] == 0 and out["context"] == []

    def test_percentile_ordinals(self):
        out = _candidate_flow(self.RIVER, {"cfs": 280}, None, None, _ref())  # p10 150, p25 300 → 23
        assert "~23rd percentile" in out["context"][0]

    def test_no_references_no_flow_scoring(self):
        out = _candidate_flow(self.RIVER, {"cfs": 99999}, None, None, None)
        assert out["delta"] == 0 and out["warnings"] == [] and out["context"] == []

    def test_lakes_untouched(self):
        lake = make_spot(type="lake")
        assert _candidate_flow(lake, {"cfs": 5}, None, None, _ref())["delta"] == 0

    def test_flood_stage_gate_and_action_penalty(self):
        flood = {"12500450": {"action_ft": 10.0, "minor_ft": 12.0}}
        at_flood = _candidate_flow(self.RIVER, {"cfs": 500, "gauge_height_ft": 12.3}, None, None, _ref(flood=flood))
        near = _candidate_flow(self.RIVER, {"cfs": 500, "gauge_height_ft": 10.5}, None, None, _ref(flood=flood))
        calm = _candidate_flow(self.RIVER, {"cfs": 500, "gauge_height_ft": 4.0}, None, None, _ref(flood=flood))
        assert at_flood["flood"] == "flood"
        assert near["flood"] == "action" and near["delta"] <= -3.0
        assert any("Near flood" in w for w in near["warnings"])
        assert calm["flood"] is None

    def test_flood_at_band_gauge_counts(self):
        flood = {"12484500": {"action_ft": 33.5, "minor_ft": 35.5}}
        out = _candidate_flow(self.RIVER, {"cfs": 9000}, None, None, _ref(_YAKIMA_BANDS, flood=flood),
                              usgs_bands=_bands_reading(9000, stage=36.0))
        assert out["flood"] == "flood"

    def test_future_trip_uses_forecast_flow_and_stage(self):
        from datetime import datetime, timedelta, timezone
        dep = datetime.now(tz=timezone.utc) + timedelta(days=3)
        fc = {"forecast": [{"validTime": dep.isoformat(), "flow_cfs": 400.0, "stage_ft": 12.5}]}
        flood = {"12500450": {"action_ft": 10.0, "minor_ft": 12.0}}
        out = _candidate_flow(self.RIVER, {"cfs": 5000, "gauge_height_ft": 1.0}, fc, dep, _ref(flood=flood))
        assert out["cfs"] == 400.0 and out["flood"] == "flood"

    def test_volatile_delta_uses_flow(self):
        flow = _candidate_flow(self.RIVER, {"cfs": 120}, None, None, _ref())
        with_flow, warnings = _compute_volatile_delta(self.RIVER, {"cfs": 120}, None, None, [], flow=flow)
        without, _ = _compute_volatile_delta(self.RIVER, {"cfs": 120}, None, None, [])
        assert with_flow == without + flow["delta"]
        assert any("Very low flow" in w for w in warnings)

    def test_band_sites_excludes_own_gauge(self):
        refs = {"a": _ref(_YAKIMA_BANDS), "b": _ref([dict(_YAKIMA_BANDS[0], gauge_site_id="12500450")])}
        assert _band_sites(refs) == {"a": {"12484500"}}

    def test_min_max_cfs_columns_no_longer_scored(self):
        import inspect
        import chat.context_builder as cb
        src = inspect.getsource(cb)
        assert "_cfs_out_of_range" not in src and ".min_cfs" not in src


# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _filter_excluded — pipeline re-run must not resurface set-aside spots
# ---------------------------------------------------------------------------

class TestFilterExcluded:
    def _pair(self, spot_id):
        fs = make_spot()
        fs.id = spot_id  # str(fs.id) is the exclusion key
        return (fs, MagicMock())

    def test_empty_excluded_returns_pairs_unchanged(self):
        pairs = [self._pair("a"), self._pair("b")]
        assert _filter_excluded(pairs, set()) is pairs

    def test_drops_excluded_spot(self):
        pairs = [self._pair("a"), self._pair("b"), self._pair("c")]
        out = _filter_excluded(pairs, {"b"})
        assert [str(fs.id) for fs, _ in out] == ["a", "c"]

    def test_drops_multiple_excluded(self):
        pairs = [self._pair("a"), self._pair("b"), self._pair("c")]
        out = _filter_excluded(pairs, {"a", "c"})
        assert [str(fs.id) for fs, _ in out] == ["b"]

    def test_excluded_id_absent_is_noop(self):
        pairs = [self._pair("a"), self._pair("b")]
        out = _filter_excluded(pairs, {"zzz"})
        assert [str(fs.id) for fs, _ in out] == ["a", "b"]
