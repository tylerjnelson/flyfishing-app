"""
Trip service — create, list, fetch, and state management.

Trip state is evaluated at session open time (§9). No user input triggers
transitions — state is derived from clock vs departure_time / return_time
and whether a debrief note exists. The computed state is written back to
trips.state on each evaluation so the sidebar always reflects current reality.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Conversation, Message, Spot, Trip, User
from intake.service import merge_intake

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State machine (§9)
# ---------------------------------------------------------------------------

def evaluate_state(trip: Trip) -> str:
    """
    Derive the current trip state from clock vs trip window + debrief presence.
    Called at session open; result written back to trips.state.

    States (§9):
      PLANNED   — departure > 24 h away
      IMMINENT  — departure within 24 h
      IN_WINDOW — now falls within [departure_time, return_time]
      POST_TRIP — return_time passed, no debrief
      DEBRIEFED — debrief_note_id is set (Phase 6 sets this)
    """
    if trip.debrief_note_id:
        return "DEBRIEFED"

    now = datetime.now(tz=timezone.utc)
    dep = trip.departure_time
    ret = trip.return_time

    if dep and ret and dep <= now <= ret:
        return "IN_WINDOW"

    if ret and now > ret:
        return "POST_TRIP"

    if dep:
        hours_until = (dep - now).total_seconds() / 3600
        if hours_until <= 24:
            return "IMMINENT"

    return "PLANNED"


async def refresh_state(trip: Trip, db: AsyncSession) -> str:
    """Evaluate state and write it back to DB if it changed."""
    computed = evaluate_state(trip)
    if trip.state != computed:
        trip.state = computed
        await db.commit()
        log.info("trip_state_updated", extra={"trip_id": str(trip.id), "state": computed})
    return computed


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

async def create_trip(
    *,
    user: User,
    session_intake: dict,
    db: AsyncSession,
) -> tuple[Trip, Conversation]:
    """
    Create a trip from the session intake card submission.

    Merges profile preferences with session intake (session overrides profile
    where both exist per §8.2). Creates the Trip record and an initial
    Conversation record for the planning chat thread.

    Returns (trip, conversation).
    """
    merged = merge_intake(user.preferences or {}, session_intake)

    # Parse departure/return times from intake
    dep_str = session_intake.get("departure_time")
    ret_str = session_intake.get("return_time")
    dep_dt = _parse_dt(dep_str)
    ret_dt = _parse_dt(ret_str)
    trip_date = dep_dt.date() if dep_dt else None

    trip = Trip(
        id=uuid.uuid4(),
        user_id=user.id,
        trip_date=trip_date,
        departure_time=dep_dt,
        return_time=ret_dt,
        session_intake=merged,
        state=evaluate_state_from_times(dep_dt, ret_dt, has_debrief=False),
    )
    db.add(trip)
    await db.flush()  # get trip.id before creating conversation

    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user.id,
        trip_id=trip.id,
        last_active=datetime.now(tz=timezone.utc),
    )
    db.add(conversation)
    await db.commit()

    log.info("trip_created", extra={"trip_id": str(trip.id), "state": trip.state})
    return trip, conversation


def evaluate_state_from_times(
    dep_dt: datetime | None,
    ret_dt: datetime | None,
    *,
    has_debrief: bool,
) -> str:
    """State evaluation without a Trip ORM object — used at creation time."""
    if has_debrief:
        return "DEBRIEFED"
    now = datetime.now(tz=timezone.utc)
    if dep_dt and ret_dt and dep_dt <= now <= ret_dt:
        return "IN_WINDOW"
    if ret_dt and now > ret_dt:
        return "POST_TRIP"
    if dep_dt and (dep_dt - now).total_seconds() / 3600 <= 24:
        return "IMMINENT"
    return "PLANNED"


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# List (sidebar)
# ---------------------------------------------------------------------------

async def list_trips(user: User, db: AsyncSession) -> dict[str, list[Trip]]:
    """
    Return trips grouped for the sidebar (§10.3).

    Evaluates and refreshes state for each trip before grouping.
    Groups: upcoming (PLANNED + IMMINENT), needs_debrief (POST_TRIP),
            past (DEBRIEFED).
    """
    result = await db.execute(
        select(Trip)
        .where(Trip.user_id == user.id)
        .order_by(Trip.departure_time.desc())
    )
    trips = result.scalars().all()

    for trip in trips:
        await refresh_state(trip, db)

    upcoming = [t for t in trips if t.state in ("PLANNED", "IMMINENT", "IN_WINDOW")]
    needs_debrief = [t for t in trips if t.state == "POST_TRIP"]
    past = [t for t in trips if t.state == "DEBRIEFED"]

    return {"upcoming": upcoming, "needs_debrief": needs_debrief, "past": past}


# ---------------------------------------------------------------------------
# Fetch detail
# ---------------------------------------------------------------------------

async def get_trip(trip_id: UUID, user_id: UUID, db: AsyncSession) -> Trip | None:
    result = await db.execute(
        select(Trip).where(Trip.id == trip_id, Trip.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def get_trip_conversation(trip_id: UUID, db: AsyncSession) -> Conversation | None:
    result = await db.execute(
        select(Conversation).where(Conversation.trip_id == trip_id)
    )
    return result.scalar_one_or_none()


async def get_conversation_messages(
    conversation_id: UUID, db: AsyncSession
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# State override (manual cancellation / admin)
# ---------------------------------------------------------------------------

async def set_trip_state(
    trip: Trip, new_state: str, db: AsyncSession
) -> Trip:
    """
    Manual state override — used for trip cancellation.
    Allowed transitions: any → POST_TRIP (cancellation without debrief).
    DEBRIEFED can only be set by the debrief pipeline (Phase 6).
    """
    allowed = {"PLANNED", "IMMINENT", "IN_WINDOW", "POST_TRIP"}
    if new_state not in allowed:
        raise ValueError(f"Manual state must be one of {allowed}")
    trip.state = new_state
    await db.commit()
    log.info("trip_state_overridden", extra={"trip_id": str(trip.id), "state": new_state})
    return trip


# ---------------------------------------------------------------------------
# Spot assignment (called by chat router after first recommendation accepted)
# ---------------------------------------------------------------------------

async def assign_spot(trip: Trip, spot_id: UUID, db: AsyncSession) -> Trip:
    """Link a recommended spot to the trip."""
    trip.spot_id = spot_id
    await db.commit()
    return trip
