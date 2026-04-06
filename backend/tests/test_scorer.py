"""
Tier 1 scorer unit tests — §11.1 priority area.

All tests exercise pure functions only (no DB, no Ollama).
Tests cover:
  - River Tier 1 weight application
  - Lake Tier 1 weight application + stocking recency step function
  - Zero-debrief fallback (river and lake)
  - Conditions-conditioned CFS similarity formula
  - seed_confidence multiplier on data_coverage signal
  - Recency penalty accumulation and cap
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from spots.scorer import (
    _norm_debrief_rating,
    _norm_note_sentiment,
    _norm_stocking_recency,
    _norm_flow_trend,
    _norm_conditions_reliability,
    _norm_species_match,
    _norm_seasonal_access,
    _norm_data_coverage,
    _recency_penalty,
    cfs_similarity,
    weighted_debrief_rating,
    score_river,
    score_lake,
)


# ---------------------------------------------------------------------------
# Signal normalisers
# ---------------------------------------------------------------------------

class TestNormalisers:
    def test_debrief_rating_min(self):
        assert _norm_debrief_rating(1.0) == 0.0

    def test_debrief_rating_max(self):
        assert _norm_debrief_rating(5.0) == 1.0

    def test_debrief_rating_midpoint(self):
        assert _norm_debrief_rating(3.0) == pytest.approx(0.5)

    def test_debrief_rating_none(self):
        assert _norm_debrief_rating(None) == 0.0

    def test_note_sentiment_all_positive(self):
        assert _norm_note_sentiment(5, 0, 0) == 1.0

    def test_note_sentiment_all_negative(self):
        assert _norm_note_sentiment(0, 0, 5) == 0.0

    def test_note_sentiment_all_neutral(self):
        assert _norm_note_sentiment(0, 5, 0) == 0.5

    def test_note_sentiment_mixed(self):
        # 2 positive (1.0), 2 neutral (0.5), 2 negative (0.0) → mean = 0.5
        assert _norm_note_sentiment(2, 2, 2) == pytest.approx(0.5)

    def test_note_sentiment_empty(self):
        assert _norm_note_sentiment(0, 0, 0) == 0.5

    def test_flow_trend_values(self):
        assert _norm_flow_trend("dropping") == 1.0
        assert _norm_flow_trend("stable") == 0.5
        assert _norm_flow_trend("rising") == 0.0
        assert _norm_flow_trend(None) == 0.5

    def test_conditions_reliability_clamps(self):
        assert _norm_conditions_reliability(0.0) == 0.0
        assert _norm_conditions_reliability(1.0) == 1.0
        assert _norm_conditions_reliability(None) == 0.0

    def test_species_match_values(self):
        assert _norm_species_match("matched") == 1.0
        assert _norm_species_match("partial") == 0.5
        assert _norm_species_match("none") == 0.0
        assert _norm_species_match(None) == 0.0

    def test_stocking_recency_step_function(self):
        assert _norm_stocking_recency(0) == 1.0
        assert _norm_stocking_recency(30) == 1.0
        assert _norm_stocking_recency(31) == 0.75
        assert _norm_stocking_recency(60) == 0.75
        assert _norm_stocking_recency(61) == 0.5
        assert _norm_stocking_recency(90) == 0.5
        assert _norm_stocking_recency(91) == 0.25
        assert _norm_stocking_recency(180) == 0.25
        assert _norm_stocking_recency(181) == 0.0
        assert _norm_stocking_recency(None) == 0.0

    def test_seasonal_access_non_alpine(self):
        assert _norm_seasonal_access(50.0, is_alpine=False) == 0.0

    def test_seasonal_access_alpine_low_snowpack(self):
        assert _norm_seasonal_access(30.0, is_alpine=True) == 1.0  # ≤50% = open

    def test_seasonal_access_alpine_high_snowpack(self):
        assert _norm_seasonal_access(120.0, is_alpine=True) == 0.0  # >100% = closed

    def test_seasonal_access_alpine_mid_snowpack(self):
        assert _norm_seasonal_access(75.0, is_alpine=True) == 0.5  # 50-100% = uncertain

    def test_seasonal_access_alpine_no_snotel(self):
        assert _norm_seasonal_access(None, is_alpine=True) == 0.5  # uncertain

    def test_data_coverage(self):
        assert _norm_data_coverage(0, 6) == 0.0
        assert _norm_data_coverage(6, 6) == 1.0
        assert _norm_data_coverage(3, 6) == pytest.approx(0.5)
        assert _norm_data_coverage(0, 0) == 0.0


# ---------------------------------------------------------------------------
# Recency penalty
# ---------------------------------------------------------------------------

class TestRecencyPenalty:
    def test_never_visited(self):
        assert _recency_penalty(None) == 0.0

    def test_visited_today(self):
        today = datetime.now(tz=timezone.utc).date()
        assert _recency_penalty(today) == 0.0

    def test_visited_14_days_ago(self):
        d = datetime.now(tz=timezone.utc).date() - timedelta(days=14)
        assert _recency_penalty(d) == pytest.approx(-0.5)

    def test_visited_28_days_ago(self):
        d = datetime.now(tz=timezone.utc).date() - timedelta(days=28)
        assert _recency_penalty(d) == pytest.approx(-1.0)

    def test_penalty_capped_at_minus_3(self):
        # 84 days = 6 periods × -0.5 = -3.0 exactly (cap)
        d = datetime.now(tz=timezone.utc).date() - timedelta(days=84)
        assert _recency_penalty(d) == pytest.approx(-3.0)

    def test_penalty_does_not_exceed_cap(self):
        d = datetime.now(tz=timezone.utc).date() - timedelta(days=365)
        assert _recency_penalty(d) == pytest.approx(-3.0)


# ---------------------------------------------------------------------------
# CFS similarity (§7.2 conditions-conditioned debrief weighting)
# ---------------------------------------------------------------------------

class TestCfsSimilarity:
    def test_identical_cfs(self):
        # snap == current → similarity = 1.0
        assert cfs_similarity(1000.0, 1000.0) == pytest.approx(1.0)

    def test_zero_current_cfs(self):
        assert cfs_similarity(1000.0, 0.0) == 0.0

    def test_none_values(self):
        assert cfs_similarity(None, 1000.0) == 0.0
        assert cfs_similarity(1000.0, None) == 0.0

    def test_50pct_deviation(self):
        # |1500 - 1000| / 1000 = 0.5 → 1 / 1.5 ≈ 0.667
        result = cfs_similarity(1500.0, 1000.0)
        assert result == pytest.approx(1 / 1.5, rel=1e-4)

    def test_100pct_deviation(self):
        # |2000 - 1000| / 1000 = 1.0 → 1 / 2.0 = 0.5
        assert cfs_similarity(2000.0, 1000.0) == pytest.approx(0.5)


class TestWeightedDebriefRating:
    def test_empty_debriefs_returns_none(self):
        assert weighted_debrief_rating([], current_cfs=1000.0) is None

    def test_simple_average_when_no_cfs(self):
        debriefs = [{"rating": 3.0, "snap_cfs": None}, {"rating": 5.0, "snap_cfs": None}]
        result = weighted_debrief_rating(debriefs, current_cfs=None)
        assert result == pytest.approx(4.0)

    def test_conditions_conditioned_weights_identical_cfs(self):
        # All snaps match current → equal weights → plain average
        debriefs = [
            {"rating": 2.0, "snap_cfs": 1000.0},
            {"rating": 4.0, "snap_cfs": 1000.0},
        ]
        result = weighted_debrief_rating(debriefs, current_cfs=1000.0)
        assert result == pytest.approx(3.0)

    def test_conditions_conditioned_weights_high_similarity_wins(self):
        # debrief A: snap=1000, rating=5.0 — high similarity to current 1000
        # debrief B: snap=5000, rating=1.0 — low similarity to current 1000
        debriefs = [
            {"rating": 5.0, "snap_cfs": 1000.0},
            {"rating": 1.0, "snap_cfs": 5000.0},
        ]
        result = weighted_debrief_rating(debriefs, current_cfs=1000.0)
        # High-similarity debrief (rating=5) should dominate
        assert result > 3.5

    def test_all_zero_similarity_falls_back_to_average(self):
        # snap_cfs=0 → cfs_similarity returns 0 for all → fallback to simple average
        debriefs = [{"rating": 2.0, "snap_cfs": 0.0}, {"rating": 4.0, "snap_cfs": 0.0}]
        result = weighted_debrief_rating(debriefs, current_cfs=500.0)
        assert result == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# River scoring — Tier 1
# ---------------------------------------------------------------------------

class TestScoreRiver:
    def test_perfect_river_score(self):
        # All signals maxed: confirmed, five-star debriefs, all positive notes,
        # dropping flow, 100% reliability, matched species, full coverage
        score = score_river(
            seed_confidence="confirmed",
            debriefs=[{"rating": 5.0, "snap_cfs": None}],
            current_cfs=None,
            flow_trend="dropping",
            note_positive=10,
            note_neutral=0,
            note_negative=0,
            conditions_reliability=1.0,
            species_match="matched",
            populated_sources=6,
            total_sources=6,
        )
        # Max signal weights: 3+2+2+2+2+0.5 = 11.5; no recency penalty
        assert score == pytest.approx(11.5, rel=1e-3)

    def test_zero_debrief_fallback_redistributes_weights(self):
        # With debriefs: debrief_rating weight=3.0
        # Without debriefs: debrief_rating weight=0.0, note_sentiment rises to 3.0
        score_with = score_river(
            seed_confidence="confirmed",
            debriefs=[{"rating": 5.0, "snap_cfs": None}],
            current_cfs=None,
            flow_trend="stable",
            note_positive=0, note_neutral=5, note_negative=0,  # neutral → 0.5
            conditions_reliability=0.5,
            species_match="partial",
            populated_sources=3,
            total_sources=6,
        )
        score_without = score_river(
            seed_confidence="confirmed",
            debriefs=[],
            current_cfs=None,
            flow_trend="stable",
            note_positive=0, note_neutral=5, note_negative=0,
            conditions_reliability=0.5,
            species_match="partial",
            populated_sources=3,
            total_sources=6,
        )
        # Scores differ because weight scheme changes
        assert score_with != score_without

    def test_zero_debrief_score_uses_fallback_weights(self):
        # With fallback: note_sentiment=3.0, conditions_reliability=3.0, data_coverage=1.0
        # All signals at 0.5 (neutral): 0.5*(0+3+2+3+2+1) = 5.5 (debrief_rating=0 so excluded)
        score = score_river(
            seed_confidence="confirmed",
            debriefs=[],
            current_cfs=None,
            flow_trend="stable",          # 0.5
            note_positive=0, note_neutral=1, note_negative=0,   # sentiment=0.5
            conditions_reliability=0.5,   # 0.5
            species_match="partial",      # 0.5
            populated_sources=3, total_sources=6,  # coverage=0.5 × 1.0 mult
        )
        expected = (
            0.0 * 0.0   # debrief_rating (weight=0 in fallback)
            + 0.5 * 3.0  # note_sentiment
            + 0.5 * 2.0  # flow_trend
            + 0.5 * 3.0  # conditions_reliability
            + 0.5 * 2.0  # species_match
            + 0.5 * 1.0  # data_coverage (confirmed=1.0 mult)
        )
        assert score == pytest.approx(expected, rel=1e-3)

    def test_seed_confidence_multiplier_on_data_coverage(self):
        base_kwargs = dict(
            debriefs=[], current_cfs=None, flow_trend=None,
            note_positive=0, note_neutral=0, note_negative=0,
            conditions_reliability=0.0, species_match=None,
            populated_sources=6, total_sources=6,
        )
        confirmed = score_river(seed_confidence="confirmed", **base_kwargs)
        probable = score_river(seed_confidence="probable", **base_kwargs)
        unvalidated = score_river(seed_confidence="unvalidated", **base_kwargs)
        # Only data_coverage differs; confirmed > probable > unvalidated
        assert confirmed > probable > unvalidated

    def test_recency_penalty_applied(self):
        no_visit = score_river(
            seed_confidence="confirmed",
            debriefs=[], current_cfs=None, flow_trend="stable",
            note_positive=5, note_neutral=0, note_negative=0,
            conditions_reliability=1.0, species_match="matched",
            populated_sources=6, total_sources=6,
            last_visited=None,
        )
        recent_visit = score_river(
            seed_confidence="confirmed",
            debriefs=[], current_cfs=None, flow_trend="stable",
            note_positive=5, note_neutral=0, note_negative=0,
            conditions_reliability=1.0, species_match="matched",
            populated_sources=6, total_sources=6,
            last_visited=datetime.now(tz=timezone.utc).date() - timedelta(days=28),
        )
        assert recent_visit < no_visit
        assert no_visit - recent_visit == pytest.approx(1.0, rel=1e-2)


# ---------------------------------------------------------------------------
# Lake scoring — Tier 1
# ---------------------------------------------------------------------------

class TestScoreLake:
    def test_recently_stocked_lake_scores_higher(self):
        fresh = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=7,
            note_positive=3, note_neutral=0, note_negative=0,
            species_match="matched",
            populated_sources=4, total_sources=6,
        )
        stale = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=200,
            note_positive=3, note_neutral=0, note_negative=0,
            species_match="matched",
            populated_sources=4, total_sources=6,
        )
        assert fresh > stale

    def test_alpine_lake_open_season_boost(self):
        open_season = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=30,
            is_alpine=True,
            snowpack_pct_of_median=20.0,   # low snow → access=1.0
            species_match="matched",
            populated_sources=4, total_sources=6,
        )
        closed_season = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=30,
            is_alpine=True,
            snowpack_pct_of_median=150.0,  # heavy snow → access=0.0
            species_match="matched",
            populated_sources=4, total_sources=6,
        )
        assert open_season > closed_season

    def test_lake_zero_debrief_fallback(self):
        with_debrief = score_lake(
            seed_confidence="confirmed",
            debriefs=[{"rating": 5.0, "snap_cfs": None}],
            days_since_stocked=30,
            note_positive=1, note_neutral=0, note_negative=0,
            species_match="matched",
            populated_sources=6, total_sources=6,
        )
        without_debrief = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=30,
            note_positive=1, note_neutral=0, note_negative=0,
            species_match="matched",
            populated_sources=6, total_sources=6,
        )
        # Weights differ; scores will differ
        assert with_debrief != without_debrief

    def test_stocking_recency_dominates_no_debrief_lake(self):
        # With zero debriefs and no notes, stocking recency (weight=3.0) is the
        # dominant signal. Fresh stock should outscore stale significantly.
        fresh = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=5,
            species_match=None,
            populated_sources=2, total_sources=6,
        )
        stale = score_lake(
            seed_confidence="confirmed",
            debriefs=[],
            days_since_stocked=365,
            species_match=None,
            populated_sources=2, total_sources=6,
        )
        assert fresh > stale + 2.0
