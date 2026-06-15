"""
Application-level hard cap on monthly HERE requests — §19.6.

HERE Technologies provides NO way to set a spend limit in its dashboard
(confirmed 2026-06-15), and the Base Plan auto-charges on overage. This module
is therefore the only thing between us and a surprise bill. It enforces a hard
ceiling of `_MONTHLY_CAP` billable HERE requests (routing + geocoding combined)
per calendar month (UTC).

The counter lives in Postgres (`here_usage_counter`, one row per month) so the
cap holds across all gunicorn workers and survives restarts — an in-process
counter would let each of the 3 workers spend the full cap independently.

`reserve(n)` atomically claims up to `n` requests against the current month and
returns how many were granted. Callers MUST honour the grant and degrade
gracefully — never surface an error to the user when the cap is hit:
  - routing fan-out (context_builder): only the granted spots call HERE; the
    remainder fall back to Haversine and trip the drive_time_unavailable banner.
  - geocoding (chat/router, users/router): granted == 0 → skip the HERE call and
    treat the location as unresolved.

The budget resets automatically at month rollover. To change the ceiling, edit
_MONTHLY_CAP below (it is a deliberate hard cap, not an env knob, so that raising
it is a reviewed code change).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from db.connection import AsyncSessionLocal

log = logging.getLogger(__name__)

# Hard cap on billable HERE requests per UTC month. HERE has no server-side spend
# limit, so this is the enforced ceiling. Raise only with intent — every request
# above the free allowance is billed.
_MONTHLY_CAP = 1000
_WARN_FRACTION = 0.8  # emit a one-time WARNING when usage crosses 80% of the cap


def _period(now: datetime | None = None) -> str:
    """Current budget bucket — UTC year-month, e.g. '2026-06'."""
    return (now or datetime.now(tz=timezone.utc)).strftime("%Y-%m")


def _grant(used: int, n: int, cap: int) -> int:
    """
    Pure cap arithmetic: how many of `n` requested HERE calls may proceed given
    `used` already spent this month against `cap`. Never negative, never lets the
    running total exceed the cap. Unit-tested in tests/test_here_budget.py.
    """
    return max(0, min(n, cap - used))


async def reserve(n: int = 1, *, cap: int = _MONTHLY_CAP) -> int:
    """
    Atomically claim up to `n` HERE requests against this month's budget and
    return the number granted (0..n).

    Never raises on exhaustion — callers fall back to Haversine / treat geocode
    as unavailable. If the budget store itself is unreachable, we fail SAFE
    (return 0, i.e. deny) rather than risk an uncapped bill.
    """
    if n <= 0:
        return 0
    period = _period()
    try:
        async with AsyncSessionLocal() as db:
            # Ensure the month row exists, then take a row lock so concurrent
            # workers cannot collectively overshoot the cap.
            await db.execute(
                text(
                    "INSERT INTO here_usage_counter (period, used) VALUES (:p, 0) "
                    "ON CONFLICT (period) DO NOTHING"
                ),
                {"p": period},
            )
            used = (
                await db.execute(
                    text("SELECT used FROM here_usage_counter WHERE period = :p FOR UPDATE"),
                    {"p": period},
                )
            ).scalar_one()

            granted = _grant(used, n, cap)
            if granted:
                await db.execute(
                    text(
                        "UPDATE here_usage_counter SET used = used + :g, updated_at = now() "
                        "WHERE period = :p"
                    ),
                    {"g": granted, "p": period},
                )
            await db.commit()
    except Exception as exc:
        # Fail safe: deny rather than risk spend if the counter is unreachable.
        log.error(
            "here_budget_unavailable",
            extra={"reason": type(exc).__name__, "detail": str(exc)[:120]},
        )
        return 0

    new_used = used + granted
    if granted < n:
        # We hit the ceiling — some/all requested calls were denied this month.
        log.warning(
            "here_budget_exhausted",
            extra={
                "period": period,
                "requested": n,
                "granted": granted,
                "used": new_used,
                "cap": cap,
            },
        )
    elif used < cap * _WARN_FRACTION <= new_used:
        # Crossed the 80% line on this reservation — heads-up before exhaustion.
        log.warning(
            "here_budget_warning",
            extra={
                "period": period,
                "used": new_used,
                "cap": cap,
                "pct": round(100 * new_used / cap),
            },
        )
    else:
        log.debug(
            "here_budget_reserved",
            extra={"period": period, "granted": granted, "used": new_used, "cap": cap},
        )
    return granted
