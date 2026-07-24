"""
Tool-call layer unit tests — Phase 5.

should_skip_planning / _coerce_args are pure/synchronous.
run_tool_planning tests mock ollama_chat + execute_tool to avoid Ollama and DB.
execute_tool dispatch is tested for the unknown-tool guard (no DB needed).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chat.tools import (
    MAX_TOOL_CALLS,
    PLANNING_NUM_CTX,
    TOOL_SCHEMAS,
    _build_planning_messages,
    _coerce_args,
    _digest_compare,
    _digest_search,
    execute_tool,
    run_tool_planning,
    should_skip_planning,
)


# ---------------------------------------------------------------------------
# Catalog sanity
# ---------------------------------------------------------------------------

def test_tool_schema_shape():
    # Phase 5: 6 tools collapsed to 3 (get_spot folds in details/conditions/notes).
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert names == {
        "get_spot",
        "compare_spots",
        "search_notes_by_text",
    }
    for t in TOOL_SCHEMAS:
        assert t["type"] == "function"
        assert "description" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# should_skip_planning
# ---------------------------------------------------------------------------

def test_skip_planning_opening_turn_always_skips():
    # No history → opening recommendation, full context already provided.
    assert should_skip_planning("compare the Sky and the Snoqualmie?", has_history=False) is True
    assert should_skip_planning("what are the regs on the Pilchuck?", has_history=False) is True


@pytest.mark.parametrize("msg", [
    "find me somewhere to fish saturday",
    "set max drive to 90 minutes",
    "the sky looked good last weekend but I want options",  # no probe keyword
    "",
    "   ",
])
def test_skip_planning_trivial(msg):
    # Even with history, trivial follow-ups skip planning.
    assert should_skip_planning(msg, has_history=True) is True


@pytest.mark.parametrize("msg", [
    "what are the regs on the Pilchuck?",          # question mark
    "compare the Sky and the Snoqualmie",          # probe: compare
    "has anyone caught fish there recently",        # probe: caught (no '?')
    "any notes from last time on the Stilly",       # probe: notes / last time
    "how are conditions looking",                   # probe: condition
    "what did you recommend earlier",               # probe: recommend
])
def test_skip_planning_needs_fetch(msg):
    # Follow-up turn (has_history) with a probe/question → run planning.
    assert should_skip_planning(msg, has_history=True) is False


# ---------------------------------------------------------------------------
# _coerce_args
# ---------------------------------------------------------------------------

def test_coerce_args_dict():
    assert _coerce_args({"spot_id": "x"}) == {"spot_id": "x"}


def test_coerce_args_json_string():
    assert _coerce_args('{"spot_id": "x"}') == {"spot_id": "x"}


def test_coerce_args_bad():
    assert _coerce_args("not json") == {}
    assert _coerce_args(None) == {}
    assert _coerce_args("[1,2,3]") == {}  # valid JSON but not an object


# ---------------------------------------------------------------------------
# execute_tool dispatch guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_tool_unknown():
    db = MagicMock()
    full, digest = await execute_tool("does_not_exist", {}, conversation_id="c", db=db)
    assert "error" in full
    assert "unknown tool" in digest


# ---------------------------------------------------------------------------
# _build_planning_messages — trimmed planning context
# ---------------------------------------------------------------------------

def test_build_planning_messages_drops_heavy_system_and_lists_spots():
    messages = [
        {"role": "system", "content": "HUGE CONDITIONS BLOCK " * 1000},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "compare the sky and stilly"},
    ]
    candidates = [
        {"spot_id": "s1", "spot_name": "Skykomish", "spot_type": "river"},
        {"spot_id": "s2", "spot_name": None, "water_body_name": "Stillaguamish", "spot_type": "river"},
    ]
    out = _build_planning_messages(messages, candidates)
    # Original heavy system message is gone; replaced with compact planner system.
    assert out[0]["role"] == "system"
    assert "HUGE CONDITIONS BLOCK" not in out[0]["content"]
    assert "Available spots:" in out[0]["content"]
    assert "s1 — Skykomish (river)" in out[0]["content"]
    assert "s2 — Stillaguamish (river)" in out[0]["content"]  # falls back to water_body_name
    # History + current user query preserved, in order.
    assert [m["role"] for m in out[1:]] == ["user", "assistant", "user"]
    assert out[-1]["content"] == "compare the sky and stilly"


def test_build_planning_messages_no_candidates():
    out = _build_planning_messages([{"role": "user", "content": "hi"}], None)
    assert out[0]["role"] == "system"
    assert "(none in current context)" in out[0]["content"]
    assert out[1]["content"] == "hi"


# ---------------------------------------------------------------------------
# run_tool_planning
# ---------------------------------------------------------------------------

def _call(name, args):
    return {"function": {"name": name, "arguments": args}}


@pytest.mark.asyncio
async def test_planning_no_tool_calls():
    """Model declines to call a tool -> empty result, no tool messages."""
    db = MagicMock()
    with patch("chat.tools.ollama_chat", new=AsyncMock(return_value={"content": "ok"})):
        res = await run_tool_planning([], conversation_id="c", db=db, model="m")
    assert res.tool_messages == []
    assert res.num_tools == 0
    assert res.planning_ms >= 0


@pytest.mark.asyncio
async def test_planning_executes_and_packages():
    db = MagicMock()
    msg = {
        "content": "",
        "tool_calls": [
            _call("get_spot", {"spot_id": "s1"}),
            _call("search_notes_by_text", '{"query": "hoppers"}'),  # JSON-string args
        ],
    }
    # Phase 5 execute_tool contract: returns (full_result, digest).
    exec_mock = AsyncMock(side_effect=lambda name, args, **kw: ({"ok": name}, f"{name}-digest"))
    chat_mock = AsyncMock(return_value=msg)
    with patch("chat.tools.ollama_chat", new=chat_mock), \
         patch("chat.tools.execute_tool", new=exec_mock):
        res = await run_tool_planning([], conversation_id="conv", db=db, model="m")

    # Planning pass uses the small context window, not the full 16K.
    assert chat_mock.await_args.kwargs["num_ctx"] == PLANNING_NUM_CTX
    assert res.num_tools == 2
    # First injected message is the assistant tool-call message, then one tool msg each.
    assert res.tool_messages[0]["role"] == "assistant"
    assert res.tool_messages[0]["tool_calls"] == msg["tool_calls"]
    tool_msgs = res.tool_messages[1:]
    assert [m["role"] for m in tool_msgs] == ["tool", "tool"]
    assert {m["tool_name"] for m in tool_msgs} == {"get_spot", "search_notes_by_text"}
    # Two-pass path injects the FULL result (not the digest) for the generation pass.
    assert json.loads(tool_msgs[0]["content"]) == {"ok": "get_spot"}
    # The JSON-string args were coerced to a dict before dispatch.
    second_call_args = exec_mock.await_args_list[1].args[1]
    assert second_call_args == {"query": "hoppers"}


@pytest.mark.asyncio
async def test_planning_caps_tool_calls():
    db = MagicMock()
    msg = {"content": "", "tool_calls": [_call("get_spot", {"spot_id": str(i)}) for i in range(7)]}
    with patch("chat.tools.ollama_chat", new=AsyncMock(return_value=msg)), \
         patch("chat.tools.execute_tool", new=AsyncMock(return_value=({}, ""))):
        res = await run_tool_planning([], conversation_id="c", db=db, model="m")
    assert res.num_tools == MAX_TOOL_CALLS
    assert len(res.tool_messages) == MAX_TOOL_CALLS + 1  # +1 assistant message


@pytest.mark.asyncio
async def test_planning_failure_is_noop():
    """A failed planning call must not raise — falls through to plain generation."""
    db = MagicMock()
    with patch("chat.tools.ollama_chat", new=AsyncMock(side_effect=RuntimeError("ollama down"))):
        res = await run_tool_planning([], conversation_id="c", db=db, model="m")
    assert res.tool_messages == []
    assert res.num_tools == 0


# ---------------------------------------------------------------------------
# get_spot — the collapsed per-spot tool (Phase 5)
# ---------------------------------------------------------------------------

def _cand(sid="s1", name="Yakima"):
    return {"fishing_spot_id": sid, "water_body_id": "w1", "spot_name": name,
            "water_body_name": name, "spot_type": "river", "conditions": {}}


@pytest.mark.asyncio
async def test_get_spot_returns_bundle_and_digest_is_the_bundle():
    # A promoted spot arrives with EVERYTHING (conditions+notes+details) via the
    # shared formatter, and its digest IS that full bundle (persists like a top-3).
    db = MagicMock()
    with patch("chat.tools._fetch_spot_notes", new=AsyncMock(return_value=[{"x": 1}])), \
         patch("chat.tools._fetch_spot_details", new=AsyncMock(return_value={"fly_fishing_legal": True})), \
         patch("chat.tools._format_spot_bundle", return_value="=== Yakima ===\nFlow: 450 CFS"):
        full, digest = await execute_tool(
            "get_spot", {"spot_id": "s1"}, conversation_id="c", db=db,
            candidates=[_cand()], active_ids=set(), excluded_ids=set(),
        )
    assert full["spot_id"] == "s1"
    assert full["name"] == "Yakima"
    assert full["bundle"] == "=== Yakima ===\nFlow: 450 CFS"
    assert digest == full["bundle"]          # digest IS the full bundle


@pytest.mark.asyncio
async def test_get_spot_active_set_short_circuits_without_fetch():
    # Spot already loaded (frozen top-3 or already promoted) → pointer, never re-fetch.
    db = MagicMock()
    notes = AsyncMock(return_value=[])
    with patch("chat.tools._fetch_spot_notes", new=notes), \
         patch("chat.tools._fetch_spot_details", new=AsyncMock(return_value={})), \
         patch("chat.tools._format_spot_bundle", return_value="BUNDLE"):
        full, digest = await execute_tool(
            "get_spot", {"spot_id": "s1"}, conversation_id="c", db=db,
            candidates=[_cand()], active_ids={"s1"}, excluded_ids=set(),
        )
    assert full["status"] == "already_in_context"
    assert digest == "Yakima is already in your context"
    notes.assert_not_awaited()               # short-circuit — no DB read


@pytest.mark.asyncio
async def test_get_spot_excluded_short_circuits():
    db = MagicMock()
    notes = AsyncMock(return_value=[])
    with patch("chat.tools._fetch_spot_notes", new=notes):
        full, digest = await execute_tool(
            "get_spot", {"spot_id": "s1"}, conversation_id="c", db=db,
            candidates=[_cand()], active_ids=set(), excluded_ids={"s1"},
        )
    assert full["status"] == "set_aside"
    assert digest == "Yakima was set aside earlier"
    notes.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_spot_not_found_is_error():
    db = MagicMock()
    with patch("chat.tools._build_candidate_from_db", new=AsyncMock(return_value=None)):
        full, digest = await execute_tool(
            "get_spot", {"spot_id": "ghost"}, conversation_id="c", db=db,
            candidates=[], active_ids=set(), excluded_ids=set(),
        )
    assert "error" in full
    assert digest == "spot not found"


@pytest.mark.asyncio
async def test_get_spot_missing_id_is_error():
    db = MagicMock()
    full, digest = await execute_tool("get_spot", {}, conversation_id="c", db=db)
    assert full["error"] == "spot_id required"


def test_digest_helpers_are_one_liners():
    assert _digest_search({"query": "hoppers", "count": 3}) == '3 note(s) match "hoppers"'
    assert _digest_search({"error": "boom"}) == "note search failed: boom"
    assert _digest_compare({"comparison": [{"name": "A"}, {"name": "B"}]}) == "compared: A, B"
    assert _digest_compare({"error": "boom"}) == "compare failed: boom"


def test_tool_layer_never_touches_here():
    """Standing constraint (spec §11.1, Phase 5 validation): the loop's HERE cost
    surface is ZERO — every tool is a pure DB read. Guard against a future
    HERE/geocoding/routing-touching tool being wired into the catalog by asserting the
    module never references those call paths. (`find_spots` never existed.)"""
    import inspect

    import chat.tools as tools_mod

    src = inspect.getsource(tools_mod)
    for banned in ("here_budget", "conditions.routing", "fetch_route", "geocode"):
        assert banned not in src, f"HERE/routing leaked into the tool layer: {banned!r}"
