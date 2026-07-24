"""
Phase 6 validation — the context-budget guard.

Compaction is deferred (2026-07-22); instead the agentic path estimates the
assembled-context size each turn (chars/4) and gracefully closes a conversation
before it overflows num_ctx (llama.cpp `-c 16384` / ollama num_ctx=16384). These
tests exercise the pure estimator + classifier (context_builder) and the router's
graceful-refusal stream, without a live engine or DB — mirroring how Phases 3/5
were validated at their boundaries.
"""

import json

import pytest

from chat import router
from chat.context_builder import (
    CONTEXT_HARD_TOKENS,
    CONTEXT_WARN_TOKENS,
    assess_context_state,
    estimate_context_tokens,
)


# ---------------------------------------------------------------------------
# estimate_context_tokens (chars/4 over content + tool_calls + tool defs)
# ---------------------------------------------------------------------------

class TestEstimateContextTokens:
    def test_empty_is_zero(self):
        assert estimate_context_tokens([]) == 0

    def test_content_only_chars_over_four(self):
        # 400 chars of content -> 100 tokens.
        msgs = [{"role": "user", "content": "x" * 400}]
        assert estimate_context_tokens(msgs) == 100

    def test_sums_across_messages(self):
        msgs = [
            {"role": "system", "content": "a" * 40},
            {"role": "user", "content": "b" * 40},
        ]
        assert estimate_context_tokens(msgs) == (40 + 40) // 4

    def test_counts_tool_calls_payload(self):
        tc = {"function": {"name": "get_spot", "arguments": {"spot_id": "abc"}}}
        msgs = [{"role": "assistant", "content": "", "tool_calls": [tc]}]
        expected = len(json.dumps(tc, default=str)) // 4
        assert estimate_context_tokens(msgs) == expected

    def test_counts_tool_definitions(self):
        tools = [{"type": "function", "function": {"name": "t"}}]
        base = estimate_context_tokens([])
        with_tools = estimate_context_tokens([], tools)
        assert with_tools == base + len(json.dumps(tools, default=str)) // 4

    def test_missing_content_key_is_safe(self):
        # A tool row may carry no content (None) — must not raise.
        assert estimate_context_tokens([{"role": "tool", "content": None}]) == 0


# ---------------------------------------------------------------------------
# assess_context_state — ok / warning / closed banding
# ---------------------------------------------------------------------------

def _msgs_for_tokens(tokens: int) -> list[dict]:
    """One message whose content estimates to exactly `tokens` (chars = 4*tokens)."""
    return [{"role": "user", "content": "x" * (tokens * 4)}]


class TestAssessContextState:
    def test_ok_below_warn(self):
        state, tokens = assess_context_state(_msgs_for_tokens(CONTEXT_WARN_TOKENS - 10))
        assert state == "ok"
        assert tokens == CONTEXT_WARN_TOKENS - 10

    def test_warning_at_threshold(self):
        state, _ = assess_context_state(_msgs_for_tokens(CONTEXT_WARN_TOKENS))
        assert state == "warning"

    def test_warning_between_thresholds(self):
        mid = (CONTEXT_WARN_TOKENS + CONTEXT_HARD_TOKENS) // 2
        state, _ = assess_context_state(_msgs_for_tokens(mid))
        assert state == "warning"

    def test_closed_at_hard_threshold(self):
        state, _ = assess_context_state(_msgs_for_tokens(CONTEXT_HARD_TOKENS))
        assert state == "closed"

    def test_closed_above_hard_threshold(self):
        state, _ = assess_context_state(_msgs_for_tokens(CONTEXT_HARD_TOKENS + 500))
        assert state == "closed"

    def test_thresholds_leave_answer_headroom(self):
        # Hard stop must sit below num_ctx with room for the answer + a reasoning
        # pass; warn must sit strictly below the hard stop.
        assert CONTEXT_WARN_TOKENS < CONTEXT_HARD_TOKENS < 16_384

    def test_deep_walk_crosses_warn_then_hard(self):
        # A conversation growing turn over turn must pass ok -> warning -> closed in
        # order (the Phase 6 "scripted deep-walk" validation, at the estimator seam).
        seen = [assess_context_state(_msgs_for_tokens(t))[0] for t in (
            1_000,                       # early turn
            CONTEXT_WARN_TOKENS + 50,    # long enough to warn
            CONTEXT_HARD_TOKENS + 50,    # deep enough to close
        )]
        assert seen == ["ok", "warning", "closed"]


# ---------------------------------------------------------------------------
# Graceful refusal stream (router) — no engine ever invoked
# ---------------------------------------------------------------------------

async def _drain(agen):
    return [json.loads(evt[len("data: "):].strip()) async for evt in agen]


class TestClosedConversationStream:
    async def test_emits_closed_then_answer_then_done(self):
        events = await _drain(router._closed_conversation_stream())
        types = [e["type"] for e in events]
        assert types == ["conversation_closed", "token", "done"]
        # The graceful copy is surfaced both as a structured signal and as a
        # readable answer token (so a client that only renders tokens still sees it).
        assert events[0]["message"] == router._CONVERSATION_CLOSED_MESSAGE
        assert events[1]["content"] == router._CONVERSATION_CLOSED_MESSAGE

    async def test_stream_touches_no_engine(self, monkeypatch):
        # The refusal path must never reach ollama/llama.cpp — assert the hop/engine
        # entrypoints are untouched while the stream is fully drained.
        called = {"hit": False}

        async def _boom(*a, **k):
            called["hit"] = True
            yield {}

        monkeypatch.setattr(router, "_stream_hop", _boom)
        monkeypatch.setattr(router, "_stream_ollama", _boom)
        await _drain(router._closed_conversation_stream())
        assert called["hit"] is False
