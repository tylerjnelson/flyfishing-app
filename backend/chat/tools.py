"""
Tool catalog for the agentic loop + two-phase planning.

All tools are pure DB reads or in-process computation.
No internet calls; no external services (the HERE cost surface for the loop is zero).

Catalog (Phase 5 — 6 tools collapsed to 3):
  - get_spot(spot_id)          — the FULL per-spot bundle (conditions + notes + details)
    in one call; replaces the old get_spot_details / get_historical_conditions /
    get_notes_for_spot. Formats via context_builder._format_spot_bundle so a promoted
    spot is byte-for-byte the shape a frozen top-3 spot had.
  - search_notes_by_text(query) — cross-corpus semantic note search.
  - compare_spots(spot_ids)     — side-by-side conditions table.

TOOL_SCHEMAS — Ollama-compatible function definitions (OpenAI format).
execute_tool() — dispatch by name, returns a ``(full_result, digest)`` tuple: the full
result is threaded into the SAME turn's context; the compact digest is what persists in
the transcript tail (Phase 2 column) for cross-turn replay.
"""

import asyncio
import json
import logging
import re
import time
import uuid as _uuid_mod
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

# Reuse the frozen-top-3 formatter + fetchers so a promoted get_spot bundle is
# byte-identical to a top-3 bundle by construction (Phase 4/5). context_builder does
# not import chat.tools, so this one-directional import is cycle-free.
from chat.context_builder import (
    _fetch_spot_details,
    _fetch_spot_notes,
    _format_spot_bundle,
)
from db.models import ConditionsCache, FishingSpot, WaterBody
from llm.client import ollama_chat
from prompts.registry import PLANNING_SYSTEM_PROMPT
from rag.embedder import embed_text

log = logging.getLogger(__name__)

# Cap on tool calls executed per turn, to bound planning latency (§Phase 5b).
MAX_TOOL_CALLS = 4
# Small context for the planning pass — it sees only a compact spot list + the
# conversation, never the full conditions block, so a large window is wasteful
# and slow on CPU. This is the main lever on planning-pass latency.
PLANNING_NUM_CTX = 4096
# Cap how many spots are listed to the planner (id + name + type only).
_PLANNING_SPOT_CAP = 12

# ---------------------------------------------------------------------------
# Tool schemas (sent to Ollama in the planning pass)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_spot",
            "description": (
                "Return the FULL profile for one fishing spot in a single call: current "
                "flow / water temp / weather, regulations, species, fly-only status, "
                "stocking dates, permit requirements, AND the group's recent trip notes. "
                "The notes carry structured detail — what was caught (species), the flies "
                "that worked, the outcome, and the approximate flow at the time — so use "
                "this to answer 'what did we catch there / what flies worked / what are "
                "the regs'. Also the way to promote a spot from the 'MORE OPTIONS' menu "
                "to full detail. One call gives everything a top recommendation already has."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spot_id": {
                        "type": "string",
                        "description": "UUID of the fishing spot (the Spot ID from the conditions block or the MORE OPTIONS menu)",
                    },
                },
                "required": ["spot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_spots",
            "description": (
                "Return a side-by-side conditions summary for 2–4 spots. "
                "Use when the user asks to compare specific spots directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spot_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of 2–4 fishing spot UUIDs to compare",
                    },
                },
                "required": ["spot_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_notes_by_text",
            "description": (
                "Search the group's trip notes by keyword or topic using semantic search. "
                "Use when the user asks a question that might be answered by past notes "
                "(e.g. 'has anyone fished the Pilchuck in April?' or 'what flies work on the Sky?')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (natural language)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum notes to return (default 5, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def execute_tool(
    name: str,
    arguments: dict,
    *,
    conversation_id: str,
    db: AsyncSession,
    candidates: list[dict] | None = None,
    active_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
) -> tuple[dict, str]:
    """
    Dispatch a tool call by name and return a ``(full_result, digest)`` tuple.

    ``full_result`` is the JSON-serialisable payload threaded into the SAME turn's
    context (the model reads it this hop); ``digest`` is the compact string that
    persists in the transcript tail for cross-turn replay. For ``get_spot`` the
    digest IS the full ``_format_spot_bundle`` (a promoted spot persists fully, like
    a top-3 spot — Phase 5/6); ``search_notes_by_text`` / ``compare_spots`` keep a
    one-line digest.

    ``candidates`` (the session's HERE-paid, pre-scored list), ``active_ids`` (spots
    already loaded in context — frozen top-3 + already-promoted), and ``excluded_ids``
    (spots the user set aside) let ``get_spot`` short-circuit instead of duplicating a
    bundle. Never raises — errors are surfaced in the result.
    """
    try:
        if name == "get_spot":
            return await _get_spot(
                db, arguments.get("spot_id"),
                candidates=candidates, active_ids=active_ids, excluded_ids=excluded_ids,
            )
        if name == "compare_spots":
            full = await _compare_spots(db, **arguments)
            return full, _digest_compare(full)
        if name == "search_notes_by_text":
            full = await _search_notes_by_text(db, **arguments)
            return full, _digest_search(full)
        log.warning("unknown_tool", extra={"tool": name})
        err = {"error": f"unknown tool: {name}"}
        return err, err["error"]
    except Exception as exc:
        log.warning("tool_execution_error", extra={"tool": name, "reason": str(exc)})
        err = {"error": str(exc)}
        return err, f"tool {name} failed: {exc}"


# ---------------------------------------------------------------------------
# Two-phase planning orchestration (Phase 5a + 5b)
# ---------------------------------------------------------------------------

# A turn is "trivial" — needing no extra fetches beyond the pre-stuffed context —
# when it neither asks a question nor probes notes / conditions / regs / history.
# Skipping the planning pass on these turns avoids ~10-30s of overhead (§Phase 5.6).
_PROBE_RE = re.compile(
    r"\b(compare|versus|vs|regulation|regs|stocked|stocking|permit|fly[- ]?only|"
    r"conditions?|flow|cfs|temperature|water\s*temp|notes?|last\s*time|previous|"
    r"recommend|you\s+(said|mentioned)|history|past\s+trip|species|hatch|"
    r"caught|access)\b",
    re.IGNORECASE,
)


def should_skip_planning(message_text: str, has_history: bool = False) -> bool:
    """
    Return True when the planning pass can be skipped entirely.

    Skipped when:
      - This is the opening turn (no prior conversation). The opening
        recommendation already receives full conditions + notes context, so a
        tool fetch is never needed — and the planning pass costs ~85s on CPU.
      - The message is trivial (no question, no probe keyword) on a later turn.

    On follow-up turns, any question mark or probe keyword forces a planning pass.
    """
    t = (message_text or "").strip()
    if not has_history:
        return True
    if not t:
        return True
    if "?" in t:
        return False
    return _PROBE_RE.search(t) is None


@dataclass
class PlanningResult:
    """Outcome of the planning pass — messages to inject + timing metrics."""

    tool_messages: list[dict] = field(default_factory=list)
    planning_ms: int = 0
    tools_ms: int = 0
    num_tools: int = 0
    skipped: bool = False


def _coerce_args(arguments) -> dict:
    """Tool-call arguments may arrive as a dict or a JSON string — normalise."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_planning_messages(
    messages: list[dict], candidates: list[dict] | None
) -> list[dict]:
    """
    Construct the trimmed context for the planning pass: a compact planning
    system prompt + a short spot list (id/name/type), followed by the
    conversation history and current user message. Deliberately drops the
    original system message (which carries the full conditions block) — the
    planner only needs to decide which tools to fetch, so re-processing every
    spot's conditions on CPU is pure latency. This is the optimisation that
    brings the planning pass back from ~280s to a few seconds.
    """
    candidates = candidates or []
    spot_lines = []
    for c in candidates[:_PLANNING_SPOT_CAP]:
        name = c.get("spot_name") or c.get("water_body_name") or c.get("name") or "unknown"
        spot_lines.append(f"{c.get('spot_id')} — {name} ({c.get('spot_type', '?')})")
    spot_block = (
        "Available spots:\n" + "\n".join(spot_lines)
        if spot_lines else "Available spots: (none in current context)"
    )
    system = PLANNING_SYSTEM_PROMPT.strip() + "\n\n" + spot_block
    # Keep history + current user query; drop the heavy original system message.
    tail = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": system}, *tail]


async def run_tool_planning(
    messages: list[dict],
    *,
    conversation_id: str,
    db: AsyncSession,
    model: str,
    candidates: list[dict] | None = None,
) -> PlanningResult:
    """
    Run the planning pass (5a) and tool execution (5b) for one chat turn.

    5a: send a TRIMMED context (compact spot list + conversation, not the full
        conditions block) plus the tool catalog to the model; it either declares
        tool calls (native function calling) or returns no calls.
    5b: execute up to MAX_TOOL_CALLS tools in parallel and package the
        assistant tool-call message plus each tool result as `tool` messages,
        ready to be appended to the generation-pass context (5c).

    Never raises — a failed planning call returns an empty (no-op) result so the
    turn falls through to a plain generation pass.
    """
    planning_messages = _build_planning_messages(messages, candidates)
    t0 = time.monotonic()
    try:
        msg = await ollama_chat(
            model, planning_messages, tools=TOOL_SCHEMAS,
            temperature=0.0, num_ctx=PLANNING_NUM_CTX,
        )
    except Exception as exc:
        log.warning("planning_pass_failed", extra={"reason": str(exc)})
        return PlanningResult(planning_ms=round((time.monotonic() - t0) * 1000))
    planning_ms = round((time.monotonic() - t0) * 1000)

    tool_calls = msg.get("tool_calls") or []
    if not tool_calls:
        return PlanningResult(planning_ms=planning_ms)

    if len(tool_calls) > MAX_TOOL_CALLS:
        log.info("planning_tool_cap", extra={"requested": len(tool_calls), "cap": MAX_TOOL_CALLS})
        tool_calls = tool_calls[:MAX_TOOL_CALLS]

    async def _run(tc: dict) -> tuple[str, dict]:
        fn = tc.get("function", {}) or {}
        name = fn.get("name", "")
        args = _coerce_args(fn.get("arguments"))
        # Two-pass path: no frozen active-set — get_spot just fetches. It sees the
        # HERE-paid candidate list so it can reuse drive time + pipeline conditions.
        full_result, _digest = await execute_tool(
            name, args, conversation_id=conversation_id, db=db, candidates=candidates,
        )
        return name, full_result

    t1 = time.monotonic()
    results = await asyncio.gather(*[_run(tc) for tc in tool_calls])
    tools_ms = round((time.monotonic() - t1) * 1000)

    # The assistant tool-call message must precede its tool results so the model
    # can pair them on the generation pass.
    tool_messages: list[dict] = [
        {"role": "assistant", "content": msg.get("content", "") or "", "tool_calls": tool_calls}
    ]
    for name, result in results:
        tool_messages.append(
            {"role": "tool", "tool_name": name, "content": json.dumps(result, default=str)}
        )

    log.info(
        "planning_complete",
        extra={
            "num_tools": len(tool_calls),
            "tools": [tc.get("function", {}).get("name") for tc in tool_calls],
            "planning_ms": planning_ms,
            "tools_ms": tools_ms,
        },
    )
    return PlanningResult(
        tool_messages=tool_messages,
        planning_ms=planning_ms,
        tools_ms=tools_ms,
        num_tools=len(tool_calls),
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _spot_name(candidate: dict) -> str:
    return (
        candidate.get("spot_name")
        or candidate.get("water_body_name")
        or candidate.get("name")
        or "that spot"
    )


def _find_candidate(candidates: list[dict] | None, spot_id: str) -> dict | None:
    """The session's HERE-paid, pre-scored candidate for this spot, if present."""
    for c in candidates or []:
        if str(c.get("fishing_spot_id")) == str(spot_id):
            return c
    return None


async def _build_candidate_from_db(db: AsyncSession, spot_id: str) -> dict | None:
    """Build a `_format_spot_bundle`-shaped candidate for a spot NOT in the session
    candidate list (e.g. referenced from history) — names + type + latest cached
    conditions, DB-only (no HERE, so no drive time)."""
    result = await db.execute(
        select(FishingSpot, WaterBody)
        .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
        .where(FishingSpot.id == _uuid_mod.UUID(spot_id))
    )
    row = result.one_or_none()
    if not row:
        return None
    fs, wb = row.FishingSpot, row.WaterBody
    cc_result = await db.execute(
        select(ConditionsCache.source, ConditionsCache.data)
        .where(ConditionsCache.water_body_id == wb.id)
        .order_by(ConditionsCache.fetched_at.desc())
        .limit(12)
    )
    conditions: dict = {}
    for cc in cc_result.mappings():
        conditions.setdefault(cc["source"], cc["data"])  # first (freshest) per source
    return {
        "fishing_spot_id": str(fs.id),
        "water_body_id": str(wb.id),
        "spot_name": fs.name or wb.name,
        "water_body_name": wb.name,
        "spot_type": wb.type,
        "drive_minutes": None,
        "is_haversine": False,
        "straight_line_miles": None,
        "warnings": [],
        "conditions": conditions,
    }


async def _get_spot(
    db: AsyncSession,
    spot_id: str | None,
    *,
    candidates: list[dict] | None = None,
    active_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
) -> tuple[dict, str]:
    """The one per-spot tool: the FULL bundle (conditions + notes + details) via the
    shared `_format_spot_bundle`, so a promoted spot is byte-for-byte the shape a
    frozen top-3 spot had. Two short-circuits run first (order per Phase 5):
      1. active set — the spot's bundle is already loaded (frozen top-3 or already
         promoted); re-appending would duplicate it. Return a pointer, don't re-fetch.
      2. excluded — the user set this spot aside; don't resurface it.
    Pure DB reads; never HERE."""
    if not spot_id:
        err = {"error": "spot_id required"}
        return err, err["error"]

    sid = str(spot_id)
    cand = _find_candidate(candidates, sid)
    display = _spot_name(cand) if cand else "that spot"

    if active_ids and sid in active_ids:
        return (
            {"spot_id": sid, "name": display, "status": "already_in_context"},
            f"{display} is already in your context",
        )
    if excluded_ids and sid in excluded_ids:
        return (
            {"spot_id": sid, "name": display, "status": "set_aside"},
            f"{display} was set aside earlier",
        )

    if cand is None:
        cand = await _build_candidate_from_db(db, sid)
    if cand is None:
        err = {"error": "spot not found"}
        return err, err["error"]

    notes = await _fetch_spot_notes(db, sid)
    details = await _fetch_spot_details(db, sid)
    bundle = _format_spot_bundle(cand, notes=notes, details=details)
    display = _spot_name(cand)
    # The digest IS the full bundle — a promoted spot persists fully in context like a
    # top-3 spot (Phase 6), not as a transient lookup.
    return {"spot_id": sid, "name": display, "bundle": bundle}, bundle


def _digest_search(full: dict) -> str:
    if "error" in full:
        return f"note search failed: {full['error']}"
    return f'{full.get("count", 0)} note(s) match "{full.get("query", "")}"'


def _digest_compare(full: dict) -> str:
    if "error" in full:
        return f"compare failed: {full['error']}"
    names = [c.get("name", "?") for c in full.get("comparison", [])]
    return "compared: " + ", ".join(names) if names else "compared (no spots)"


async def _compare_spots(db: AsyncSession, spot_ids: list) -> dict:
    spot_ids = [str(s) for s in (spot_ids or [])][:4]
    if len(spot_ids) < 2:
        return {"error": "provide at least 2 spot_ids"}

    uuid_objs = [_uuid_mod.UUID(s) for s in spot_ids]

    # Get names
    fs_result = await db.execute(
        select(FishingSpot.id, FishingSpot.name, WaterBody.name.label("wb_name"), WaterBody.type)
        .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
        .where(FishingSpot.id.in_(uuid_objs))
    )
    names = {
        str(r.id): r.name or r.wb_name
        for r in fs_result
    }

    # Get latest conditions for each spot's water body
    wb_result = await db.execute(
        select(FishingSpot.id, FishingSpot.water_body_id)
        .where(FishingSpot.id.in_(uuid_objs))
    )
    wb_map = {str(r.id): r.water_body_id for r in wb_result}

    comparison = []
    for spot_id in spot_ids:
        entry: dict = {"spot_id": spot_id, "name": names.get(spot_id, "unknown")}
        wb_id = wb_map.get(spot_id)
        if wb_id:
            cc_result = await db.execute(
                select(ConditionsCache.source, ConditionsCache.data)
                .where(ConditionsCache.water_body_id == wb_id)
                .where(ConditionsCache.source == "usgs")
                .order_by(ConditionsCache.fetched_at.desc())
                .limit(1)
            )
            cc_row = cc_result.mappings().one_or_none()
            if cc_row:
                data = cc_row["data"] or {}
                entry["cfs"] = data.get("cfs")
                entry["cfs_trend"] = data.get("trend")
                entry["water_temp_f"] = data.get("temp_f")
        comparison.append(entry)

    return {"comparison": comparison}


async def _search_notes_by_text(db: AsyncSession, query: str, limit: int = 5) -> dict:
    limit = min(int(limit), 10)
    try:
        embedding = await embed_text(query)
    except Exception as exc:
        log.warning("tool_embed_failed", extra={"reason": str(exc)})
        return {"error": f"embedding unavailable: {exc}"}

    embedding_str = f"[{','.join(str(v) for v in embedding)}]"
    sql = text("""
        SELECT n.id, n.content, n.note_date, n.outcome, n.species,
               n.fishing_spot_id,
               (n.embedding <=> CAST(:embedding AS vector)) AS distance
        FROM notes n
        WHERE n.source_type != 'map'
          AND n.embedding IS NOT NULL
        ORDER BY distance ASC
        LIMIT :limit
    """)
    result = await db.execute(sql, {"embedding": embedding_str, "limit": limit})
    notes = []
    for row in result.mappings():
        notes.append({
            "date": str(row["note_date"]) if row["note_date"] else None,
            "outcome": row["outcome"],
            "species": list(row["species"] or []),
            "spot_id": str(row["fishing_spot_id"]) if row["fishing_spot_id"] else None,
            "content": (row["content"] or "")[:500],
        })
    return {"query": query, "notes": notes, "count": len(notes)}
