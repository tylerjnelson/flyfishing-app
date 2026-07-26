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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from chat.context_builder import assess_context_state, build_context
from chat.response_cache import store_response
from chat.streaming import StreamHandler
from chat.tools import (
    MAX_TOOL_CALLS,
    TOOL_SCHEMAS,
    _coerce_args,
    execute_tool,
    run_tool_planning,
    should_skip_planning,
)
from chat.turn_builder import build_turn
from conditions import here_budget
from config import settings
from db.connection import get_db
from db.models import Conversation, Message, Note, Trip, User
from llm.client import CHAT_MODEL
from notes.ingestion import ingest_note_task
from trips.service import assign_spot, get_trip, get_trip_conversation, refresh_state

log = logging.getLogger(__name__)

router = APIRouter()


async def _next_seq(db: AsyncSession, conversation_id) -> int:
    """Next per-conversation transcript ordinal (max(seq)+1, or 0 if empty).

    Transcript order comes from `seq`, not `created_at` — see Message.seq. A
    single conversation's turns are serialized (one user turn at a time), so
    max(seq)+1 is sufficient without a DB sequence.
    """
    result = await db.execute(
        select(func.max(Message.seq)).where(Message.conversation_id == conversation_id)
    )
    current = result.scalar()
    return (current + 1) if current is not None else 0

_OLLAMA_CHAT_URL = "/api/chat"
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# Ollama chat streaming
# ---------------------------------------------------------------------------

async def _stream_ollama(messages: list[dict]):
    """
    Yield raw token strings from the chat engine (streaming), then a sentinel
    dict {"_done": True, "token_count": n} with usage stats.

    Named for its original ollama /api/chat path; under the llama.cpp engine
    (Phase 1) it delegates to the llama-server chat instance, which yields the
    same token-then-sentinel contract. reasoning_content (thinking) is excluded
    there exactly as ollama's `thinking` is here.
    """
    if settings.chat_engine == "llamacpp":
        from llm import llamacpp
        async for item in llamacpp.stream_chat(
            messages, base_url=settings.llama_chat_url, temperature=0.7,
        ):
            yield item
        return

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
# Agentic loop (Phase 3) — behind FLYFISH_HARNESS_MODE=agentic
# ---------------------------------------------------------------------------

# Max tool hops per turn before we force a content answer. Each hop is a full
# reasoning pass (Phase 0b — reasoning stays ON), so this directly multiplies
# latency; keep it small. MAX_TOOL_CALLS (chat/tools.py) still caps calls per hop.
HOP_CAP = 3

# User-facing labels emitted as `tool_status` SSE while a hop runs, so the ~20-200s
# silent reasoning/fetch phase isn't a dead stream (Phase 3 / §Phase 7 UX).
_TOOL_STATUS_LABELS = {
    "get_spot": "Looking up spot details…",
    "compare_spots": "Comparing spots…",
    "search_notes_by_text": "Searching notes…",
}

# Frozen top-N whose bundles are already in the prefix (Phase 4 `_SURFACE_TOP_N`).
# Their spot_ids seed the active set so get_spot short-circuits instead of
# re-appending a bundle that's already in context (Phase 5 active-set dedup).
_FROZEN_TOP_N = 3

# Context-budget guard (Phase 6). User-facing copy for the two thresholds. The
# warning is a soft nudge streamed alongside a normal answer; the closed message
# is streamed INSTEAD of an answer when a turn would overflow num_ctx (see
# assess_context_state / chat_endpoint). Agentic mode only — twopass is untouched.
_CONTEXT_WARNING_MESSAGE = (
    "This conversation is getting long. When you're ready, start a new trip "
    "conversation to keep responses fast and accurate."
)
_CONVERSATION_CLOSED_MESSAGE = (
    "This conversation has reached its length limit — please start a new trip "
    "conversation to continue."
)


async def _closed_conversation_stream():
    """SSE generator that gracefully refuses a turn on a length-closed conversation
    (Phase 6 hard stop): the closed message rendered as a normal answer token, then
    `done`. No context is built and the engine is never invoked, so a turn whose
    assembled context would exceed the prefill budget is never sent."""
    yield _sse({"type": "conversation_closed", "message": _CONVERSATION_CLOSED_MESSAGE})
    yield _sse({"type": "token", "content": _CONVERSATION_CLOSED_MESSAGE})
    yield _sse({"type": "done"})


def _seed_active_ids(messages: list[dict], candidates: list[dict]) -> set[str]:
    """The active set = spots whose bundle is already loaded: the frozen top-3 plus
    any spot already promoted via get_spot earlier in this conversation (read off the
    replayed transcript's assistant tool_calls rows — Phase 2 schema). Derived, never
    stored (Phase 5): the transcript is the single source of truth."""
    ids = {
        str(c["fishing_spot_id"])
        for c in candidates[:_FROZEN_TOP_N]
        if c.get("fishing_spot_id")
    }
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                if fn.get("name") == "get_spot":
                    sid = _coerce_args(fn.get("arguments")).get("spot_id")
                    if sid:
                        ids.add(str(sid))
    return ids


def _dedup_get_spot(tool_calls: list[dict]) -> list[dict]:
    """Drop duplicate get_spot calls for the same spot_id within one hop, so two
    concurrent promotions of the same spot can't both miss the active-set check and
    append the bundle twice (asyncio.gather runs them without seeing each other)."""
    seen: set[str] = set()
    out: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        if fn.get("name") == "get_spot":
            sid = str(_coerce_args(fn.get("arguments")).get("spot_id"))
            if sid in seen:
                continue
            seen.add(sid)
        out.append(tc)
    return out


async def _stream_hop(messages: list[dict], *, tools: list[dict] | None):
    """
    Stream one loop hop from the chat engine with the tool catalog enabled.

    Yields content token strings, then a sentinel
    ``{"_done": True, "token_count": n, "tool_calls": [...], "finish_reason": str}``.
    ``tool_calls`` is the ollama shape ``[{"function": {"name", "arguments"}}]`` —
    assembled from streaming ``delta.tool_calls`` fragments on llama.cpp (Phase 0e
    E3), or read whole from ``message.tool_calls`` on ollama (Phase 0). A tool hop
    streams zero user-facing content (Phase 0), so no token leaks before dispatch.
    """
    if settings.chat_engine == "llamacpp":
        from llm import llamacpp
        async for item in llamacpp.stream_chat(
            messages, base_url=settings.llama_chat_url, tools=tools, temperature=0.7,
        ):
            yield item
        return

    payload: dict = {
        "model": CHAT_MODEL,
        "messages": messages,
        "stream": True,
        "options": {"temperature": 0.7},
        "keep_alive": -1,
    }
    if tools:
        payload["tools"] = tools
    token_count = 0
    tool_calls: list[dict] = []
    finish_reason = None
    async with httpx.AsyncClient(
        base_url=settings.ollama_base_url, timeout=_STREAM_TIMEOUT
    ) as client:
        async with client.stream("POST", _OLLAMA_CHAT_URL, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                msg = chunk.get("message", {}) or {}
                # ollama delivers each tool call whole (Phase 0), already in the
                # {"function": {"name", "arguments": dict}} shape callers expect.
                for tc in msg.get("tool_calls") or []:
                    tool_calls.append(tc)
                token = msg.get("content", "")
                if token:
                    token_count += 1
                    yield token
                if chunk.get("done"):
                    yield {
                        "_done": True,
                        "token_count": token_count,
                        "tool_calls": tool_calls,
                        "finish_reason": chunk.get("done_reason"),
                    }
                    return


async def _persist_tool_turn(db, conversation_id, tool_calls, results) -> None:
    """
    Append one tool turn to the persisted transcript (Phase 2 schema): a single
    assistant row carrying the ``tool_calls``, then one ``role="tool"`` row per
    result. ``results`` is a list of ``(name, full_result, digest)`` (Phase 5
    execute_tool contract): the full result is what the model saw THIS turn (stored
    as ``content``), while ``digest`` — the compact form (for get_spot, the full
    bundle; for search/compare, a one-liner) — is what the follow-up context reads
    and replays. Each row is committed before the next so ``_next_seq`` sees it (seq
    is the load-bearing transcript order — see Message.seq).
    """
    assistant_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        role="assistant",
        content="",
        tool_calls=tool_calls,
        seq=await _next_seq(db, conversation_id),
    )
    db.add(assistant_msg)
    await db.commit()
    for name, full_result, digest in results:
        tool_msg = Message(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            role="tool",
            tool_name=name,
            content=json.dumps(full_result, default=str),
            digest=digest,
            seq=await _next_seq(db, conversation_id),
        )
        db.add(tool_msg)
        await db.commit()


async def _run_agentic_loop(*, messages, handler, conversation, trip, db, candidates=None):
    """
    The single tool-calling loop (Phase 3) — replaces the two-pass block. Yields
    SSE strings (tokens + `tool_status`); drives `handler` on the content hop and
    persists each tool turn to the transcript tail.

    Per hop the model either returns ``tool_calls`` (execute them, thread the
    assistant call + tool results into the running context AND persist them, loop)
    or streams the answer (drive through StreamHandler, stop). Reasoning arrives on
    a channel the engine adapter never forwards, so only ``content`` is streamed; a
    "Thinking…" status covers the silent reasoning phase. HOP_CAP bounds tool hops;
    if it is hit with the model still asking for tools, one final tools-omitted hop
    forces an answer so a turn never ends on a dangling tool call.

    ``get_spot`` (Phase 5) short-circuits on the active set — the frozen top-3 plus
    spots already promoted (seeded from the replayed transcript, grown in-turn as
    each bundle is appended) — and on the conversation's excluded spots, so a bundle
    is never duplicated in the tail and a set-aside spot is never resurfaced.
    """
    messages = list(messages)  # local copy — we append tool turns as we go
    candidates = candidates or []
    active_ids = _seed_active_ids(messages, candidates)
    excluded_ids = {str(s) for s in (conversation.excluded_spot_ids or [])}
    t_start = time.monotonic()
    t_first_token = None
    total_tokens = 0
    hops_used = 0
    tools_used: list[str] = []
    answered = False

    async def _run(tc: dict) -> tuple[str, dict, str]:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        args = _coerce_args(fn.get("arguments"))
        full_result, digest = await execute_tool(
            name, args, conversation_id=str(conversation.id), db=db,
            candidates=candidates, active_ids=active_ids, excluded_ids=excluded_ids,
        )
        return name, full_result, digest

    for _hop in range(HOP_CAP):
        yield _sse({"type": "tool_status", "label": "Thinking…"})
        sentinel = None
        async for item in _stream_hop(messages, tools=TOOL_SCHEMAS):
            if isinstance(item, dict) and item.get("_done"):
                sentinel = item
                break
            if t_first_token is None:
                t_first_token = time.monotonic()
            text = handler.process_token(item)
            if text:
                yield _sse({"type": "token", "content": text})
        total_tokens += (sentinel or {}).get("token_count", 0)
        tool_calls = (sentinel or {}).get("tool_calls") or []

        if not tool_calls:
            answered = True
            break

        # --- tool hop ---
        hops_used += 1
        if len(tool_calls) > MAX_TOOL_CALLS:
            log.info("agentic_tool_cap", extra={"requested": len(tool_calls), "cap": MAX_TOOL_CALLS})
            tool_calls = tool_calls[:MAX_TOOL_CALLS]
        tool_calls = _dedup_get_spot(tool_calls)

        for tc in tool_calls:
            name = (tc.get("function") or {}).get("name", "")
            tools_used.append(name)
            yield _sse({"type": "tool_status", "label": _TOOL_STATUS_LABELS.get(name, "Working…")})

        results = await asyncio.gather(*[_run(tc) for tc in tool_calls])

        # Every get_spot target joins the active set so a later hop dedups against it
        # (short-circuited or freshly fetched — either way its bundle is now in the tail).
        for tc in tool_calls:
            fn = tc.get("function") or {}
            if fn.get("name") == "get_spot":
                sid = _coerce_args(fn.get("arguments")).get("spot_id")
                if sid:
                    active_ids.add(str(sid))

        # Thread the tool turn into the running context for the next hop...
        messages.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        for name, full_result, _digest in results:
            messages.append(
                {"role": "tool", "tool_name": name, "content": json.dumps(full_result, default=str)}
            )
        # ...and persist it (transcript-persistence slice of Phase 4; Phase 5 digests).
        await _persist_tool_turn(db, conversation.id, tool_calls, results)

    if not answered:
        # HOP_CAP exhausted with the model still calling tools — force a content
        # answer with tools omitted so the turn never ends on a dangling tool call.
        yield _sse({"type": "tool_status", "label": "Thinking…"})
        sentinel = None
        async for item in _stream_hop(messages, tools=None):
            if isinstance(item, dict) and item.get("_done"):
                sentinel = item
                break
            if t_first_token is None:
                t_first_token = time.monotonic()
            text = handler.process_token(item)
            if text:
                yield _sse({"type": "token", "content": text})
        total_tokens += (sentinel or {}).get("token_count", 0)

    generation_ms = round((time.monotonic() - t_start) * 1000)
    ttft_ms = round((t_first_token - t_start) * 1000) if t_first_token else None
    log.info(
        f"agentic_loop_complete hops={hops_used} num_tools={len(tools_used)} "
        f"gen_ms={generation_ms} ttft_ms={ttft_ms} tokens={total_tokens}",
        extra={
            "ttft_ms": ttft_ms,
            "hops": hops_used,
            "num_tools": len(tools_used),
            "tools": tools_used,
            "generation_ms": generation_ms,
            "token_count": total_tokens,
            "trip_id": str(trip.id),
        },
    )


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

    # Context-budget guard (Phase 6, agentic only). A conversation already closed
    # on a prior turn refuses every new turn up front — no context built, no engine
    # call, no user row persisted (the turn never happens).
    if settings.harness_mode == "agentic" and conversation.context_state == "closed":
        log.info("conversation_closed_refused", extra={"conversation_id": str(conversation.id)})
        return StreamingResponse(_closed_conversation_stream(), media_type="text/event-stream")

    # Build context (steps 1-7 or cache hit)
    build_result = await build_context(
        user=user,
        trip=trip,
        conversation=conversation,
        query=message_text,
        db=db,
    )

    # Context-budget guard (Phase 6, agentic only): estimate the assembled context
    # for THIS turn (system prompt + frozen prefix + transcript tail + tool defs).
    # At/above the hard stop it would overflow num_ctx and the engine would silently
    # truncate the frozen prefix — so close the conversation and refuse this turn
    # before it is sent (nothing persisted). The warn band runs normally but emits a
    # context_warning so the UI can nudge toward a new trip conversation.
    emit_context_warning = False
    if settings.harness_mode == "agentic":
        state, est_tokens = assess_context_state(build_result.messages, TOOL_SCHEMAS)
        if state != (conversation.context_state or "ok"):
            conversation.context_state = state
            await db.commit()
        if state == "closed":
            log.info(
                "conversation_closed_budget",
                extra={"conversation_id": str(conversation.id), "est_tokens": est_tokens},
            )
            return StreamingResponse(_closed_conversation_stream(), media_type="text/event-stream")
        emit_context_warning = state == "warning"

    # Persist user message
    user_msg = Message(
        id=uuid.uuid4(),
        conversation_id=conversation.id,
        role="user",
        content=message_text,
        seq=await _next_seq(db, conversation.id),
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

        # Context-budget warning (Phase 6): this turn is in the warn band — answer
        # normally, but nudge the UI toward a new trip conversation.
        if emit_context_warning:
            yield _sse({"type": "context_warning", "message": _CONTEXT_WARNING_MESSAGE})

        # Cache hit — emit full response as single token, persist, then done.
        # Persist BEFORE `done` for the same client-disconnect reason as the main
        # path below (a client closing on `done` would otherwise drop this row).
        if build_result.cached_response:
            log.info("response_cache_hit_served")
            yield _sse({"type": "token", "content": build_result.cached_response})

            assistant_msg = Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="assistant",
                content=build_result.cached_response,
                seq=await _next_seq(db, conversation.id),
            )
            db.add(assistant_msg)
            await db.commit()

            yield _sse({"type": "done"})
            return

        messages = build_result.messages
        candidates = build_result.session_candidates.get("candidates", [])
        handler = StreamHandler(candidates)

        if settings.harness_mode == "agentic":
            # Single tool-calling loop (Phase 3). Instant rollback to the two-pass
            # path via FLYFISH_HARNESS_MODE=twopass.
            async for evt in _run_agentic_loop(
                messages=messages,
                handler=handler,
                conversation=conversation,
                trip=trip,
                db=db,
                candidates=candidates,
            ):
                yield evt
        else:
            # Two-pass planning + generation (pre-Phase-3 path). Planning pass
            # (Phase 5a/5b) lets the model fetch extra context before generation;
            # skipped for trivial turns; failures fall through to a plain generation
            # pass. Tool results are appended as `tool` messages (5c).
            planning_ms = 0
            tools_ms = 0
            num_tools = 0
            # History exists when there are turns beyond [system, current_user].
            has_history = len(messages) > 2
            if not should_skip_planning(message_text, has_history=has_history):
                plan = await run_tool_planning(
                    messages,
                    conversation_id=str(conversation.id),
                    db=db,
                    model=CHAT_MODEL,
                    candidates=candidates,
                )
                planning_ms = plan.planning_ms
                tools_ms = plan.tools_ms
                num_tools = plan.num_tools
                if plan.tool_messages:
                    messages = messages + plan.tool_messages

            # Stream from Ollama
            t_start = time.monotonic()
            t_first_token = None

            async for chunk in _stream_ollama(messages):
                # Sentinel from generator
                if isinstance(chunk, dict) and chunk.get("_done"):
                    token_count = chunk.get("token_count", 0)
                    generation_ms = round((time.monotonic() - t_start) * 1000)
                    ttft_ms = round((t_first_token - t_start) * 1000) if t_first_token else None
                    # Timings are embedded in the message string because the JSON log
                    # formatter only renders `message` (extra fields are dropped) —
                    # this keeps the Phase 5 planning/tools/generation split visible.
                    log.info(
                        f"llm_stream_complete planning_ms={planning_ms} tools_ms={tools_ms} "
                        f"num_tools={num_tools} gen_ms={generation_ms} ttft_ms={ttft_ms} "
                        f"tokens={token_count}",
                        extra={
                            "ttft_ms": ttft_ms,
                            "planning_ms": planning_ms,
                            "tools_ms": tools_ms,
                            "num_tools": num_tools,
                            "generation_ms": generation_ms,
                            "total_ms": planning_ms + tools_ms + generation_ms,
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

        # Turn builder — assemble spot cards from [RECOMMEND: ...] block
        if handler.recommend_block:
            turn = await build_turn(
                narrative=handler.full_response,
                recommend_block=handler.recommend_block,
                candidates=build_result.session_candidates.get("candidates", []),
                db=db,
            )
            if turn.get("cards"):
                yield _sse({"type": "spot_cards", "cards": turn["cards"]})
            elif turn.get("error"):
                log.warning("turn_builder_error", extra={"error": turn["error"], "trip_id": str(trip.id)})

        # ---- Persist BEFORE signalling `done` ----
        # These writes must complete regardless of client behaviour. If `done` is
        # yielded first, a client that closes the socket the instant it sees `done`
        # cancels this async generator before `db.commit()` runs — dropping the
        # assistant row plus the FILTER_UPDATE / SAVE_NOTE / response-cache side
        # effects (all rolled back with the uncommitted session). Committing here,
        # ahead of `done`, costs a few ms of latency before the client sees `done`
        # but guarantees durability. (Agentic tool turns are already persisted
        # mid-loop via _persist_tool_turn.)

        # Persist assistant message
        full_response = handler.full_response
        if full_response:
            assistant_msg = Message(
                id=uuid.uuid4(),
                conversation_id=conversation.id,
                role="assistant",
                content=full_response,
                seq=await _next_seq(db, conversation.id),
            )
            db.add(assistant_msg)

            # Store in response cache (top candidate)
            if build_result.conditions_hash and build_result.intake_hash:
                candidates = build_result.session_candidates.get("candidates", [])
                if candidates:
                    await store_response(
                        db, candidates[0]["fishing_spot_id"],
                        build_result.conditions_hash, build_result.intake_hash,
                        full_response,
                    )

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

        # All turn state durably committed above — safe to signal completion.
        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# POST /api/chat/exclude-spot
# ---------------------------------------------------------------------------

@router.post("/chat/exclude-spot")
async def exclude_spot_endpoint(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Remove a spot from session_candidates and record it as excluded.

    Body: {conversation_id, spot_id}

    - Validates spot_id is present in conversation.session_candidates.
    - Removes it from the candidates list.
    - Appends to conversation.excluded_spot_ids.
    - Returns the updated candidate list.
    """
    conversation_id = body.get("conversation_id")
    spot_id = body.get("spot_id")
    if not conversation_id or not spot_id:
        raise HTTPException(status_code=400, detail="conversation_id and spot_id required")

    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    candidates = (conversation.session_candidates or {}).get("candidates", [])
    if not any(c.get("spot_id") == spot_id for c in candidates):
        raise HTTPException(status_code=404, detail="Spot not in session candidates")

    updated_candidates = [c for c in candidates if c.get("spot_id") != spot_id]
    conversation.session_candidates = {
        **(conversation.session_candidates or {}),
        "candidates": updated_candidates,
    }

    existing_excluded = list(conversation.excluded_spot_ids or [])
    if spot_id not in {str(e) for e in existing_excluded}:
        existing_excluded.append(uuid.UUID(spot_id))
    conversation.excluded_spot_ids = existing_excluded

    # Bust the frozen prefix (Phase 4): removing a menu entry changes the byte-stable
    # block, so the next turn must re-freeze. Harmless under twopass (column unused).
    conversation.frozen_context = None
    conversation.frozen_context_at = None

    await db.commit()
    log.info("spot_excluded", extra={"spot_id": spot_id, "conversation_id": str(conversation_id)})
    return {"candidates": updated_candidates}


# ---------------------------------------------------------------------------
# POST /api/chat/commit-spot
# ---------------------------------------------------------------------------

@router.post("/chat/commit-spot")
async def commit_spot_endpoint(
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lock a recommended spot in as the trip's chosen spot (the "lock this spot" flow).

    Body: {conversation_id, spot_id}

    - Validates spot_id is present in conversation.session_candidates.
    - Resolves that candidate's fishing_spot_id (the real FishingSpot UUID — candidates
      carry both the card key `spot_id` and `fishing_spot_id`; assign_spot needs the
      latter, and resolving it via the candidate sidesteps the legacy water_body-id
      staleness).
    - Sets trip.fishing_spot_id via assign_spot. Purely additive: does NOT close the
      conversation or change trip state; re-locking simply overwrites.
    """
    conversation_id = body.get("conversation_id")
    spot_id = body.get("spot_id")
    if not conversation_id or not spot_id:
        raise HTTPException(status_code=400, detail="conversation_id and spot_id required")

    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    candidates = (conversation.session_candidates or {}).get("candidates", [])
    match = next((c for c in candidates if c.get("spot_id") == spot_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail="Spot not in session candidates")

    fishing_spot_id = match.get("fishing_spot_id") or match.get("spot_id")

    trip = await get_trip(conversation.trip_id, user.id, db)
    if not trip:
        raise HTTPException(status_code=404, detail="Trip not found")

    await assign_spot(trip, uuid.UUID(str(fishing_spot_id)), db)
    log.info(
        "spot_committed",
        extra={
            "fishing_spot_id": str(fishing_spot_id),
            "conversation_id": str(conversation_id),
            "trip_id": str(trip.id),
        },
    )
    return {"fishing_spot_id": str(fishing_spot_id), "spot_id": spot_id}


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
    # HERE kill-switch (batch/benchmark — spec §11.1): never call HERE geocoding.
    if settings.here_disabled:
        log.warning("geocode_skipped_here_disabled", extra={"query": query})
        return None
    # Counts against the monthly HERE budget (§19.6). If the cap is exhausted,
    # skip the HERE call and treat the location as unresolved.
    if await here_budget.reserve(1) == 0:
        log.warning("geocode_skipped_budget", extra={"query": query})
        return None
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
