"""
Phase 3 validation — the agentic tool-calling loop and its transcript replay.

The loop (chat/router.py `_run_agentic_loop`) is exercised with the streaming
engine mocked at the `_stream_hop` boundary (post-adapter: content strings +
sentinel), so these tests are engine-agnostic and need no live server. Tool
execution and transcript persistence are mocked too — this isolates the loop's
control flow (hops, cap, forced answer, status/token SSE) from the DB.

The read side (context_builder `_rebuild_transcript`) is tested directly: the
digest-replay slice must reconstruct tool turns from persisted rows while leaving
plain user/assistant history byte-identical to the pre-Phase-3 read (so the
two-pass path is unaffected).
"""

import json
import types
import uuid

import pytest

from chat import router
from chat.context_builder import _rebuild_transcript
from chat.streaming import StreamHandler
from chat.tools import MAX_TOOL_CALLS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sentinel(tool_calls=None, tokens=0, finish="stop"):
    return {
        "_done": True,
        "token_count": tokens,
        "tool_calls": tool_calls or [],
        "finish_reason": finish,
    }


def _tool_call(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args or {}}}


def _fake_stream_hop(scripts):
    """Return an async `_stream_hop` replacement that yields `scripts[i]` on the
    i-th call, plus a record of the `tools` arg each call saw."""
    state = {"i": 0, "tools_seen": []}

    async def _hop(messages, *, tools):
        script = scripts[state["i"]]
        state["i"] += 1
        state["tools_seen"].append(tools)
        for item in script:
            yield item

    return _hop, state


async def _run_loop(monkeypatch, scripts):
    """Drive `_run_agentic_loop` with a scripted stream + mocked tool exec/persist.
    Returns (events, handler, exec_calls, persisted, hop_state)."""
    hop, hop_state = _fake_stream_hop(scripts)
    exec_calls = []
    persisted = []

    async def _fake_execute_tool(name, arguments, **kwargs):
        # Phase 5 contract: execute_tool returns (full_result, digest).
        exec_calls.append((name, arguments))
        return {"tool": name, "ok": True}, f"{name}-digest"

    async def _fake_persist(db, conversation_id, tool_calls, results):
        persisted.append((tool_calls, results))

    monkeypatch.setattr(router, "_stream_hop", hop)
    monkeypatch.setattr(router, "execute_tool", _fake_execute_tool)
    monkeypatch.setattr(router, "_persist_tool_turn", _fake_persist)

    handler = StreamHandler([])
    conv = types.SimpleNamespace(id=uuid.uuid4(), excluded_spot_ids=None)
    trip = types.SimpleNamespace(id=uuid.uuid4())

    events = []
    async for evt in router._run_agentic_loop(
        messages=[{"role": "user", "content": "hi"}],
        handler=handler,
        conversation=conv,
        trip=trip,
        db=None,
    ):
        assert evt.startswith("data: ")
        events.append(json.loads(evt[len("data: "):].strip()))

    return events, handler, exec_calls, persisted, hop_state


def _tokens(events):
    return [e["content"] for e in events if e["type"] == "token"]


def _statuses(events):
    return [e["label"] for e in events if e["type"] == "tool_status"]


# ---------------------------------------------------------------------------
# Loop control flow
# ---------------------------------------------------------------------------

class TestAgenticLoop:
    async def test_zero_hop_answer_streams_content_no_tools(self, monkeypatch):
        scripts = [["Hello ", "world", _sentinel()]]
        events, handler, exec_calls, persisted, hop_state = await _run_loop(monkeypatch, scripts)

        assert _tokens(events) == ["Hello ", "world"]
        assert handler.full_response == "Hello world"
        assert exec_calls == []           # no tool executed
        assert persisted == []            # nothing persisted to the transcript
        assert "Thinking…" in _statuses(events)   # reasoning-phase status emitted
        assert hop_state["i"] == 1        # exactly one hop

    async def test_single_fetch_then_answer(self, monkeypatch):
        scripts = [
            [_sentinel(tool_calls=[_tool_call("get_spot")])],
            ["Try the Yakima.", _sentinel()],
        ]
        events, handler, exec_calls, persisted, hop_state = await _run_loop(monkeypatch, scripts)

        assert [c[0] for c in exec_calls] == ["get_spot"]
        assert len(persisted) == 1                      # one tool turn persisted
        assert _tokens(events) == ["Try the Yakima."]
        assert handler.full_response == "Try the Yakima."
        # tool-specific status surfaced between hops
        assert "Looking up spot details…" in _statuses(events)
        assert hop_state["i"] == 2

    async def test_multi_hop_two_fetches_then_answer(self, monkeypatch):
        scripts = [
            [_sentinel(tool_calls=[_tool_call("compare_spots")])],
            [_sentinel(tool_calls=[_tool_call("search_notes_by_text")])],
            ["Done.", _sentinel()],
        ]
        events, handler, exec_calls, persisted, hop_state = await _run_loop(monkeypatch, scripts)

        assert [c[0] for c in exec_calls] == ["compare_spots", "search_notes_by_text"]
        assert len(persisted) == 2
        assert handler.full_response == "Done."
        assert hop_state["i"] == 3

    async def test_hop_cap_forces_tools_omitted_answer(self, monkeypatch):
        # HOP_CAP tool hops, each still asking for a tool -> a final tools-omitted
        # hop is forced so the turn never ends on a dangling tool call.
        scripts = [
            [_sentinel(tool_calls=[_tool_call("get_spot")])]
            for _ in range(router.HOP_CAP)
        ]
        scripts.append(["Forced answer.", _sentinel()])
        events, handler, exec_calls, persisted, hop_state = await _run_loop(monkeypatch, scripts)

        assert len(exec_calls) == router.HOP_CAP          # a tool ran each capped hop
        assert len(persisted) == router.HOP_CAP
        assert handler.full_response == "Forced answer."
        assert hop_state["i"] == router.HOP_CAP + 1       # + the forced hop
        # the forced hop ran with tools omitted; every prior hop had tools.
        assert hop_state["tools_seen"][-1] is None
        assert all(t is not None for t in hop_state["tools_seen"][:-1])

    async def test_per_hop_tool_cap(self, monkeypatch):
        # A single hop returning more than MAX_TOOL_CALLS is capped.
        many = [_tool_call("search_notes_by_text", args={"query": str(i)}, cid=f"c{i}")
                for i in range(MAX_TOOL_CALLS + 3)]
        scripts = [
            [_sentinel(tool_calls=many)],
            ["ok", _sentinel()],
        ]
        events, handler, exec_calls, persisted, hop_state = await _run_loop(monkeypatch, scripts)

        assert len(exec_calls) == MAX_TOOL_CALLS          # capped, not 7
        assert len(persisted[0][1]) == MAX_TOOL_CALLS     # persisted turn also capped

    async def test_duplicate_get_spot_in_one_hop_deduped(self, monkeypatch):
        # Two get_spot(X) in the SAME hop must collapse to one (can't both miss the
        # active-set check and append the bundle twice — Phase 5).
        dupes = [
            _tool_call("get_spot", args={"spot_id": "X"}, cid="c1"),
            _tool_call("get_spot", args={"spot_id": "X"}, cid="c2"),
            _tool_call("get_spot", args={"spot_id": "Y"}, cid="c3"),
        ]
        scripts = [[_sentinel(tool_calls=dupes)], ["ok", _sentinel()]]
        _, _, exec_calls, persisted, _ = await _run_loop(monkeypatch, scripts)
        # X executed once, Y once — the duplicate X dropped.
        assert sorted(a["spot_id"] for _, a in exec_calls) == ["X", "Y"]
        assert len(persisted[0][1]) == 2

    async def test_content_only_hop_no_persistence(self, monkeypatch):
        # A hop that answers immediately must not persist any tool row.
        scripts = [["just an answer", _sentinel()]]
        _, _, exec_calls, persisted, _ = await _run_loop(monkeypatch, scripts)
        assert exec_calls == []
        assert persisted == []


# ---------------------------------------------------------------------------
# Digest replay (read side of the persistence slice)
# ---------------------------------------------------------------------------

def _row(role, content=None, tool_name=None, tool_calls=None, digest=None):
    return types.SimpleNamespace(
        role=role, content=content, tool_name=tool_name,
        tool_calls=tool_calls, digest=digest,
    )


class TestTranscriptReplay:
    def test_plain_history_is_byte_identical_to_pre_phase3(self):
        # Rows with no tool columns rebuild exactly as the old {role, content}
        # read did -> the two-pass path is unaffected.
        rows = [
            _row("user", "where to fish?"),
            _row("assistant", "the Yakima"),
        ]
        assert _rebuild_transcript(rows) == [
            {"role": "user", "content": "where to fish?"},
            {"role": "assistant", "content": "the Yakima"},
        ]

    def test_tool_turn_rebuilds_structurally(self):
        tc = [{"function": {"name": "get_spot_details", "arguments": {"spot_id": "a"}}}]
        rows = [
            _row("user", "regs on the Yak?"),
            _row("assistant", "", tool_calls=tc),
            _row("tool", content='{"regs": "fly-only"}', tool_name="get_spot_details",
                 digest='{"regs": "fly-only"}'),
            _row("assistant", "It's fly-only."),
        ]
        assert _rebuild_transcript(rows) == [
            {"role": "user", "content": "regs on the Yak?"},
            {"role": "assistant", "content": "", "tool_calls": tc},
            {"role": "tool", "tool_name": "get_spot_details", "content": '{"regs": "fly-only"}'},
            {"role": "assistant", "content": "It's fly-only."},
        ]

    def test_tool_row_replays_digest_over_content(self):
        # The cross-turn replay uses `digest` (the compact form), not raw `content`.
        rows = [_row("tool", content="RAW_FULL_PAYLOAD", tool_name="t", digest="COMPACT")]
        assert _rebuild_transcript(rows) == [
            {"role": "tool", "tool_name": "t", "content": "COMPACT"}
        ]
