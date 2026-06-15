"""
Unit tests for the HERE monthly spend-cap arithmetic (§19.6).

These cover the pure `_grant` logic that decides how many HERE calls may proceed
given the month's running total — the enforcement guarantee that we never exceed
the cap. The DB plumbing in reserve() is a thin atomic wrapper around this.
"""

from conditions import here_budget
from conditions.here_budget import _grant


CAP = here_budget._MONTHLY_CAP


def test_default_cap_is_1000():
    # Hard cap is intentionally fixed in code — guard against accidental change.
    assert CAP == 1000


def test_grant_full_when_budget_available():
    assert _grant(used=0, n=50, cap=CAP) == 50
    assert _grant(used=500, n=50, cap=CAP) == 50


def test_grant_partial_at_the_boundary():
    # 10 left in the budget, 50 requested → only 10 granted.
    assert _grant(used=CAP - 10, n=50, cap=CAP) == 10


def test_grant_zero_when_exhausted():
    assert _grant(used=CAP, n=50, cap=CAP) == 0
    # Never negative even if a prior over-count somehow pushed past the cap.
    assert _grant(used=CAP + 25, n=50, cap=CAP) == 0


def test_grant_never_exceeds_cap_cumulatively():
    used = 0
    for _ in range(100):
        used += _grant(used=used, n=37, cap=CAP)
    assert used == CAP


def test_grant_zero_request():
    assert _grant(used=0, n=0, cap=CAP) == 0


def test_period_is_utc_year_month():
    from datetime import datetime, timezone

    assert here_budget._period(datetime(2026, 6, 15, 23, 0, tzinfo=timezone.utc)) == "2026-06"
