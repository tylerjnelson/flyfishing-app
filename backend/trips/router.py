"""
Trips API — §10.2.

POST /api/trips           — create trip from session intake card
GET  /api/trips           — list trips grouped for sidebar
GET  /api/trips/{trip_id} — trip detail + conversation messages
PATCH /api/trips/{trip_id}/state — manual state override (cancellation)
"""

import logging
import uuid
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.connection import get_db
from db.models import Note, Spot, User
from sqlalchemy import select
from trips.service import (
    assign_spot,
    create_trip,
    get_conversation_messages,
    get_trip,
    get_trip_conversation,
    list_trips,
    refresh_state,
    set_trip_state,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------

def _trip_summary(trip) -> dict:
    return {
        "id": str(trip.id),
        "trip_date": trip.trip_date.isoformat() if trip.trip_date else None,
        "departure_time": trip.departure_time.isoformat() if trip.departure_time else None,
        "return_time": trip.return_time.isoformat() if trip.return_time else None,
        "state": trip.state,
        "spot_id": str(trip.spot_id) if trip.spot_id else None,
        "session_intake": trip.session_intake or {},
    }


def _message_out(msg) -> dict:
    return {
        "id": str(msg.id),
        "role": msg.role,
        "content": msg.content,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_trip_endpoint(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a trip from session intake card submission.

    Body: session intake answers (departure_time, return_time, water_type,
    target_species, trip_goal, trail_difficulty). Profile preferences from
    user.preferences are merged in automatically.
    """
    trip, conversation = await create_trip(
        user=user,
        session_intake=body,
        db=db,
    )
    return {
        "trip_id": str(trip.id),
        "conversation_id": str(conversation.id),
        "state": trip.state,
    }


@router.get("")
async def list_trips_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Return trips grouped for the sidebar (§10.3).

    Groups:
      upcoming      — PLANNED + IMMINENT + IN_WINDOW (highlighted if IMMINENT)
      needs_debrief — POST_TRIP
      past          — DEBRIEFED
    """
    grouped = await list_trips(user, db)

    def serialise_group(trips):
        out = []
        for t in trips:
            s = _trip_summary(t)
            s["highlight"] = t.state == "IMMINENT"
            s["needs_debrief_dot"] = t.state == "POST_TRIP"
            out.append(s)
        return out

    return {
        "upcoming": serialise_group(grouped["upcoming"]),
        "needs_debrief": serialise_group(grouped["needs_debrief"]),
        "past": serialise_group(grouped["past"]),
    }


@router.get("/{trip_id}")
async def get_trip_endpoint(
    trip_id: UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch trip detail + conversation messages.

    State is refreshed on each fetch so the UI always reflects the current
    state without requiring a separate state-sync call.
    """
    trip = await get_trip(trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    state = await refresh_state(trip, db)
    conversation = await get_trip_conversation(trip_id, db)
    messages = []
    conversation_id = None
    session_candidates = None
    drive_time_unavailable = False

    if conversation:
        conversation_id = str(conversation.id)
        messages = await get_conversation_messages(conversation.id, db)
        raw_candidates = conversation.session_candidates
        if isinstance(raw_candidates, dict):
            session_candidates = raw_candidates.get("candidates")
            drive_time_unavailable = raw_candidates.get("drive_time_unavailable", False)

    # Fetch spot name if linked
    spot_name = None
    if trip.spot_id:
        spot_result = await db.execute(select(Spot).where(Spot.id == trip.spot_id))
        spot = spot_result.scalar_one_or_none()
        spot_name = spot.name if spot else None

    return {
        "trip": {
            **_trip_summary(trip),
            "spot_name": spot_name,
        },
        "state": state,
        "conversation_id": conversation_id,
        "drive_time_unavailable": drive_time_unavailable,
        "session_candidates": session_candidates,
        "messages": [_message_out(m) for m in messages],
    }


@router.post("/{trip_id}/debrief", status_code=201)
async def save_debrief_endpoint(
    trip_id: UUID,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Save a trip debrief.

    Serialises the conversation, summarises it via LLM using DEBRIEF_SUMMARY_PROMPT,
    stores the prose as a debrief note, sets trip.debrief_note_id (which transitions
    the trip to DEBRIEFED), and fires note ingestion (field extraction + spot
    resolution + embedding + immediate re-score) as a BackgroundTask.

    Only valid when trip.state == POST_TRIP.
    """
    from llm.client import CHAT_MODEL, ollama_generate
    from notes.ingestion import ingest_note_task
    from prompts.registry import DEBRIEF_SUMMARY_PROMPT

    trip = await get_trip(trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    await refresh_state(trip, db)
    if trip.state != "POST_TRIP":
        raise HTTPException(
            status_code=400,
            detail=f"Debrief requires POST_TRIP state, got {trip.state}",
        )

    conversation = await get_trip_conversation(trip_id, db)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await get_conversation_messages(conversation.id, db)
    if not messages:
        raise HTTPException(status_code=400, detail="No conversation to debrief")

    # Serialise conversation for the summarisation prompt
    conversation_text = "\n".join(
        f"{m.role.upper()}: {m.content}" for m in messages
    )

    # Summarise with Llama 3.1 8B (prose, not JSON — §18.5)
    prose_summary = await ollama_generate(
        DEBRIEF_SUMMARY_PROMPT.format(conversation_text=conversation_text),
        model=CHAT_MODEL,
    )

    # Create the debrief note
    note = Note(
        id=uuid.uuid4(),
        content=prose_summary,
        source_type="debrief",
        author_id=user.id,
        trip_id=trip.id,
    )
    db.add(note)
    await db.flush()

    # Setting debrief_note_id auto-transitions trip to DEBRIEFED (§9)
    trip.debrief_note_id = note.id
    await db.commit()

    # Fire ingestion: field extraction + spot resolution + embedding + score
    background_tasks.add_task(ingest_note_task, note.id, "debrief", user.id)

    log.info(
        "debrief_saved",
        extra={"trip_id": str(trip_id), "note_id": str(note.id)},
    )
    return {
        "note_id": str(note.id),
        "trip_id": str(trip_id),
        "state": "DEBRIEFED",
    }


@router.patch("/{trip_id}/state")
async def set_state_endpoint(
    trip_id: UUID,
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Manual state override — used for trip cancellation.

    Body: {"state": "POST_TRIP"}

    Only POST_TRIP is accepted as a manual override. DEBRIEFED is set
    exclusively by the debrief pipeline (Phase 6).
    """
    new_state = body.get("state")
    if new_state not in ("POST_TRIP",):
        raise HTTPException(
            status_code=400,
            detail="Manual state override only supports POST_TRIP (cancellation).",
        )

    trip = await get_trip(trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    trip = await set_trip_state(trip, new_state, db)
    return {"trip_id": str(trip.id), "state": trip.state}
