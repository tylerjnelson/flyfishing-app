"""
Tool catalog for Phase 5 two-phase planning.

All tools are pure DB reads or in-process computation.
No internet calls; no external services.

TOOL_SCHEMAS — Ollama-compatible function definitions (OpenAI format).
execute_tool() — dispatch by name, return JSON-serialisable result dict.
"""

import json
import logging
import re
import uuid as _uuid_mod

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ConditionsCache, FishingSpot, Message, Note, WaterBody
from rag.embedder import embed_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas (sent to Ollama in the planning pass)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_notes_for_spot",
            "description": (
                "Fetch the group's recent trip notes for a specific fishing spot. "
                "Use this when the user asks about past experience at a named spot, "
                "or when you want to reference specific note content for a recommendation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spot_id": {
                        "type": "string",
                        "description": "UUID of the fishing spot (from the Spot ID in the conditions block)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum notes to return (default 5, max 10)",
                    },
                },
                "required": ["spot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_historical_conditions",
            "description": (
                "Return the latest cached conditions for a spot's water body "
                "(flow, temperature, weather). Use when the user asks about current "
                "or recent conditions for a specific spot not covered in your context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spot_id": {
                        "type": "string",
                        "description": "UUID of the fishing spot",
                    },
                },
                "required": ["spot_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_spot_details",
            "description": (
                "Return full details for a spot: regulations, species, fly-only status, "
                "stocking dates, permit requirements. Use when the user asks about "
                "regulations or access specifics for a particular spot."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "spot_id": {
                        "type": "string",
                        "description": "UUID of the fishing spot",
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
    {
        "type": "function",
        "function": {
            "name": "get_my_previous_recommendations",
            "description": (
                "Return the spots you recommended in earlier turns of this conversation. "
                "Use to maintain continuity when the user refers to 'your recommendations' "
                "or 'the spots you mentioned'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "UUID of the current conversation",
                    },
                },
                "required": ["conversation_id"],
            },
        },
    },
]

_RE_RECOMMEND = re.compile(r'\[RECOMMEND:\s*([^\]]+)\]', re.IGNORECASE)

# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def execute_tool(
    name: str,
    arguments: dict,
    *,
    conversation_id: str,
    db: AsyncSession,
) -> dict:
    """
    Dispatch a tool call by name and return a JSON-serialisable result dict.
    Always returns a dict — never raises (errors are surfaced in the result).
    """
    try:
        if name == "get_notes_for_spot":
            return await _get_notes_for_spot(db, **arguments)
        if name == "get_historical_conditions":
            return await _get_historical_conditions(db, **arguments)
        if name == "get_spot_details":
            return await _get_spot_details(db, **arguments)
        if name == "compare_spots":
            return await _compare_spots(db, **arguments)
        if name == "search_notes_by_text":
            return await _search_notes_by_text(db, **arguments)
        if name == "get_my_previous_recommendations":
            return await _get_my_previous_recommendations(
                db, arguments.get("conversation_id") or conversation_id
            )
        log.warning("unknown_tool", extra={"name": name})
        return {"error": f"unknown tool: {name}"}
    except Exception as exc:
        log.warning("tool_execution_error", extra={"name": name, "reason": str(exc)})
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def _get_notes_for_spot(db: AsyncSession, spot_id: str, limit: int = 5) -> dict:
    limit = min(int(limit), 10)
    result = await db.execute(
        select(
            Note.id,
            Note.content,
            Note.note_date,
            Note.outcome,
            Note.species,
            Note.approx_cfs,
            Note.source_type,
        )
        .where(Note.fishing_spot_id == _uuid_mod.UUID(spot_id))
        .where(Note.source_type != "map")
        .order_by(Note.note_date.desc().nullslast())
        .limit(limit)
    )
    notes = []
    for row in result.mappings():
        notes.append({
            "date": str(row["note_date"]) if row["note_date"] else None,
            "outcome": row["outcome"],
            "species": list(row["species"] or []),
            "approx_cfs": row["approx_cfs"],
            "content": (row["content"] or "")[:500],
        })
    return {"spot_id": spot_id, "notes": notes, "count": len(notes)}


async def _get_historical_conditions(db: AsyncSession, spot_id: str) -> dict:
    # Get the water_body_id for the spot, then fetch latest conditions_cache entries
    wb_result = await db.execute(
        select(FishingSpot.water_body_id).where(FishingSpot.id == _uuid_mod.UUID(spot_id))
    )
    row = wb_result.one_or_none()
    if not row:
        return {"error": "spot not found"}

    water_body_id = row.water_body_id
    cc_result = await db.execute(
        select(ConditionsCache.source, ConditionsCache.data, ConditionsCache.fetched_at)
        .where(ConditionsCache.water_body_id == water_body_id)
        .order_by(ConditionsCache.fetched_at.desc())
        .limit(4)
    )
    conditions = []
    for cc in cc_result.mappings():
        conditions.append({
            "source": cc["source"],
            "fetched_at": cc["fetched_at"].isoformat() if cc["fetched_at"] else None,
            "data": cc["data"],
        })
    return {"spot_id": spot_id, "conditions": conditions}


async def _get_spot_details(db: AsyncSession, spot_id: str) -> dict:
    result = await db.execute(
        select(FishingSpot, WaterBody)
        .join(WaterBody, FishingSpot.water_body_id == WaterBody.id)
        .where(FishingSpot.id == _uuid_mod.UUID(spot_id))
    )
    row = result.one_or_none()
    if not row:
        return {"error": "spot not found"}

    fs, wb = row.FishingSpot, row.WaterBody
    return {
        "spot_id": spot_id,
        "name": fs.name or wb.name,
        "water_body_name": wb.name,
        "spot_type": wb.type,
        "fly_fishing_legal": wb.fly_fishing_legal,
        "fishing_regs": wb.fishing_regs,
        "species_primary": list(wb.species_primary or []),
        "last_stocked_date": str(wb.last_stocked_date) if wb.last_stocked_date else None,
        "last_stocked_species": list(wb.last_stocked_species or []),
        "permit_required": fs.permit_required,
        "permit_notes": fs.permit_notes,
        "last_visited": str(fs.last_visited) if fs.last_visited else None,
    }


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


async def _get_my_previous_recommendations(
    db: AsyncSession, conversation_id: str
) -> dict:
    result = await db.execute(
        select(Message.content, Message.created_at)
        .where(Message.conversation_id == _uuid_mod.UUID(conversation_id))
        .where(Message.role == "assistant")
        .order_by(Message.created_at.asc())
    )
    all_recommendations: list[dict] = []
    for row in result.mappings():
        content = row["content"] or ""
        m = re.search(_RE_RECOMMEND, content)
        if m:
            raw_ids = [s.strip() for s in m.group(1).split(",") if s.strip()]
            all_recommendations.append({
                "turn_at": row["created_at"].isoformat() if row["created_at"] else None,
                "spot_ids": raw_ids,
            })
    latest = all_recommendations[-1] if all_recommendations else None
    return {
        "conversation_id": conversation_id,
        "recommendation_history": all_recommendations,
        "latest": latest,
    }
