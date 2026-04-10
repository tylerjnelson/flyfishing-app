"""
Chat router — §10.2, §6.5.

POST /api/chat                — stream Ollama response via SSE
POST /api/chat/confirm-filter — accept or reject a pending FILTER_UPDATE
GET  /health/models           — auth-gated Ollama model status
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from chat.context_builder import build_context
from chat.response_cache import store_response
from chat.streaming import StreamHandler
from config import settings
from db.connection import get_db
from db.models import Conversation, Message, Note, Spot, Trip, User
from llm.client import CHAT_MODEL
from notes.ingestion import ingest_note_task
from trips.service import get_trip, get_trip_conversation, refresh_state

log = logging.getLogger(__name__)

router = APIRouter()

_OLLAMA_CHAT_URL = "/api/chat"
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# Ollama chat streaming
# ---------------------------------------------------------------------------

async def _stream_ollama(messages: list[dict]):
    """
    Yield raw token strings from Ollama /api/chat (streaming).
    Also yields a special sentinel dict at the end with usage stats.
    """
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7},
        "keep_alive": -1,
    }
    token_count = 0
    async with httpx.AsyncClient(
        base_url=settings.ollama_base_url, timeout=_STREAM_TIMEOUT
    ) as client:
        async with client.stream("POST", _OLLAMA_CHAT_URL, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                token = chunk.get("message", {}).get("content", "")
                if token:
                    token_count += 1
                    yield token
                if chunk.get("done"):
                    yield {"_done": True, "token_count": token_count}
                    return


# ---------------------------------------------------------------------------
# POST /api/chat
# ---------------------------------------------------------------------------

@router.post("/chat")
async def chat_endpoint(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stream an Ollama recommendation response for a trip conversation.

    Body: {conversation_id, message}

    SSE event types emitted:
      {"type": "token", "content": "..."}          — LLM output token
      {"type": "filter_confirmation_required",
       "key": "max_drive_minutes", "value": "90"}  — FILTER_UPDATE intercepted
      {"type": "drive_time_unavailable"}            — HERE fell back to Haversine
      {"type": "done"}                              — stream complete
    """
    conversation_id = body.get("conversation_id")
    message_text = body.get("message", "").strip()
    if not conversation_id or not message_text:
        raise HTTPException(status_code=400, detail="conversation_id and message required")

    # Fetch conversation + trip
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    trip = await get_trip(conversation.trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    await refresh_state(trip, db)

    # Build context (steps 1-7 or cache hit)
    build_result = await build_context(
        user=user,
        trip=trip,
        conversation=conversation,
        query=message_text,
        db=db,
    )

    # Persist user message
    user_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        role="user",
        content=message_text,
    )
    db.add(user_msg)
    await db.commit()

    # Persist updated session_candidates
    conversation.session_candidates = build_result.session_candidates
    conversation.last_active = datetime.now(tz=timezone.utc)
    await db.commit()

    async def event_stream():
        # Drive-time unavailable banner
        if build_result.drive_time_unavailable:
            yield _sse({"type": "drive_time_unavailable"})

        # Cache hit — emit full response as single token, then done
        if build_result.cached_response:
            log.info("response_cache_hit_served")
            yield _sse({"type": "token", "content": build_result.cached_response})
            yield _sse({"type": "done"})

            assistant_msg = Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="assistant",
                content=build_result.cached_response,
            )
            db.add(assistant_msg)
            await db.commit()
            return

        # Stream from Ollama
        handler = StreamHandler(
            build_result.session_candidates.get("candidates", [])
        )
        t_start = time.monotonic()
        t_first_token = None

        async for chunk in _stream_ollama(build_result.messages):
            # Sentinel from generator
            if isinstance(chunk, dict) and chunk.get("_done"):
                token_count = chunk.get("token_count", 0)
                total_ms = round((time.monotonic() - t_start) * 1000)
                ttft_ms = round((t_first_token - t_start) * 1000) if t_first_token else None
                log.info(
                    "llm_stream_complete",
                    extra={
                        "ttft_ms": ttft_ms,
                        "total_ms": total_ms,
                        "token_count": token_count,
                        "trip_id": str(trip.id),
                    },
                )
                break

            # Record time to first token
            if t_first_token is None:
                t_first_token = time.monotonic()

            text = handler.process_token(chunk)
            if text:
                yield _sse({"type": "token", "content": text})

        # Flush remaining buffer
        remaining = handler.flush_remaining()
        if remaining:
            yield _sse({"type": "token", "content": remaining})

        # Final SSE event (FILTER_UPDATE confirmation)
        final_event = handler.on_stream_end()
        if final_event:
            yield _sse({"type": final_event["event"], "key": final_event["key"], "value": final_event["value"]})

        yield _sse({"type": "done"})

        # ---- Post-stream DB writes ----

        # Persist assistant message
        full_response = handler.full_response
        if full_response:
            assistant_msg = Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="assistant",
                content=full_response,
            )
            db.add(assistant_msg)

            # Store in response cache (top candidate)
            if build_result.conditions_hash:
                candidates = build_result.session_candidates.get("candidates", [])
                if candidates:
                    await store_response(
                        db, candidates[0]["spot_id"],
                        build_result.conditions_hash, full_response,
                    )

        # Update session_candidates after EXCLUDE_SPOT
        if handler.excluded_spot_ids:
            updated = handler.advance_candidates()
            conversation.session_candidates = {
                **build_result.session_candidates,
                "candidates": updated,
            }

        # Promote or re-insert SURFACE_ALTERNATE spot
        if handler.surface_alternate:
            current_candidates = (conversation.session_candidates or {}).get("candidates", [])
            updated, found = handler.apply_surface_alternate(current_candidates)
            if not found:
                # Spot was hard-filtered — fetch from DB and re-insert with caveat
                alt_spot_id = handler.surface_alternate["spot_id"]
                spot_res = await db.execute(select(Spot).where(Spot.id == alt_spot_id))
                alt_spot = spot_res.scalar_one_or_none()
                if alt_spot:
                    caveat_candidate = {
                        "spot_id": str(alt_spot.id),
                        "spot_name": alt_spot.name,
                        "spot_type": alt_spot.type,
                        "session_score": 0.0,
                        "drive_minutes": None,
                        "is_haversine": False,
                        "straight_line_miles": None,
                        "last_visited": (
                            alt_spot.last_visited.isoformat() if alt_spot.last_visited else None
                        ),
                        "conditions": {},
                        "surfaced_with_caveat": True,
                    }
                    updated = [caveat_candidate] + current_candidates
                else:
                    log.debug(
                        "surface_alternate_spot_not_found",
                        extra={"spot_id": alt_spot_id},
                    )
            conversation.session_candidates = {
                **(conversation.session_candidates or {}),
                "candidates": updated,
            }

        # Write FILTER_UPDATE to conversation for confirm-filter endpoint
        if handler.pending_filter_update:
            conversation.pending_filter_update = handler.pending_filter_update

        conversation.last_active = datetime.now(tz=timezone.utc)
        await db.commit()

        # Ingest any SAVE_NOTE contents
        for note_text in handler.save_note_contents:
            note = Note(
                id=uuid.uuid4(),
                content=note_text,
                source_type="typed",
                author_id=user.id,
                trip_id=trip.id,
            )
            db.add(note)
            await db.commit()
            asyncio.create_task(
                ingest_note_task(note.id, "typed", user.id)
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# POST /api/chat/confirm-filter
# ---------------------------------------------------------------------------

@router.post("/chat/confirm-filter")
async def confirm_filter_endpoint(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Accept or reject a pending FILTER_UPDATE.

    Body: {conversation_id, confirm: true|false}

    Yes (confirm=true):
      - Applies the filter key/value to trip.session_intake
      - Geocodes departure_location if key == "departure_location"
      - Re-runs the full pipeline (force_rerun=True) → replaces session_candidates
      - Clears conversations.pending_filter_update

    No (confirm=false):
      - Clears conversations.pending_filter_update
      - session_candidates unchanged
    """
    conversation_id = body.get("conversation_id")
    confirm = body.get("confirm", False)

    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    pending = conversation.pending_filter_update
    if not pending:
        raise HTTPException(status_code=400, detail="No pending filter update")

    trip = await get_trip(conversation.trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    if not confirm:
        # No — clear pending, leave session_candidates unchanged
        conversation.pending_filter_update = None
        await db.commit()
        return {"result": "rejected", "session_candidates": conversation.session_candidates}

    # Yes — apply filter to trip.session_intake, re-run pipeline
    key = pending.get("key")
    value = pending.get("value")

    intake = dict(trip.session_intake or {})

    if key == "departure_location":
        # Geocode the location string before storing
        coords = await _geocode_location(value)
        if coords:
            intake["departure_location"] = coords
        else:
            log.warning("geocode_failed", extra={"query": value})
            intake["departure_location"] = {"label": value, "lat": None, "lon": None}
    elif key == "max_drive_minutes":
        try:
            intake["max_drive_minutes"] = int(value)
        except (ValueError, TypeError):
            log.debug("filter_update_bad_value", extra={"key": key, "value": value})
    elif key == "water_type":
        intake["water_type"] = [v.strip() for v in value.split(",")]
    else:
        intake[key] = value

    trip.session_intake = intake
    conversation.pending_filter_update = None
    await db.commit()

    # Pipeline re-run with updated intake
    build_result = await build_context(
        user=user,
        trip=trip,
        conversation=conversation,
        query="",
        db=db,
        force_rerun=True,
    )

    conversation.session_candidates = build_result.session_candidates
    await db.commit()

    candidates = build_result.session_candidates.get("candidates", [])
    return {
        "result": "accepted",
        "filter_applied": {key: value},
        "session_candidates": candidates[:5],
        "drive_time_unavailable": build_result.drive_time_unavailable,
    }


async def _geocode_location(query: str) -> dict | None:
    """Geocode a free-text location string via HERE Geocoding API."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.get(
                "https://geocode.search.hereapi.com/v1/geocode",
                params={"q": query, "in": "countryCode:USA", "apiKey": settings.here_api_key},
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            if not items:
                return None
            pos = items[0].get("position", {})
            label = items[0].get("title", query)
            return {"lat": pos.get("lat"), "lon": pos.get("lng"), "label": label}
    except Exception as exc:
        log.warning("geocode_error", extra={"query": query, "reason": str(exc)})
        return None


# ---------------------------------------------------------------------------
# GET /health/models
# ---------------------------------------------------------------------------

@router.get("/health/models")
async def health_models(
    _: User = Depends(get_current_user),
):
    """
    Return currently loaded Ollama models and keep_alive values.
    Auth-gated — not publicly accessible (§10.2).
    """
    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url,
            timeout=httpx.Timeout(5.0),
        ) as client:
            resp = await client.get("/api/ps")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ollama unavailable: {exc}")
