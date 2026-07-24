"""
Unit tests for the llama.cpp adapter (Phase 1, Option B).

No live server: the pure ollama<->OpenAI translation functions are tested
directly (that is where the round-trip risk lives), and chat()/complete()/
stream_chat() are exercised against a fake httpx.AsyncClient so the
request-build + response-map paths are covered without a network.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from llm import llamacpp


# ---------------------------------------------------------------------------
# _to_openai_messages
# ---------------------------------------------------------------------------

def test_plain_messages_passthrough():
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert llamacpp._to_openai_messages(msgs) == msgs


def test_assistant_tool_calls_dict_args_to_openai():
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_spot", "arguments": {"spot_id": "abc"}}}],
        },
    ]
    out = llamacpp._to_openai_messages(msgs)
    tc = out[0]["tool_calls"][0]
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "get_spot"
    # arguments must be a JSON *string* for the OpenAI endpoint
    assert json.loads(tc["function"]["arguments"]) == {"spot_id": "abc"}
    assert tc["id"]  # a stable id was minted


def test_tool_result_paired_to_call_id():
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_spot", "arguments": {"spot_id": "a"}}}],
        },
        {"role": "tool", "tool_name": "get_spot", "content": '{"ok": true}'},
    ]
    out = llamacpp._to_openai_messages(msgs)
    minted_id = out[0]["tool_calls"][0]["id"]
    tool_msg = out[1]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == minted_id  # paired by order via the FIFO
    assert tool_msg["name"] == "get_spot"


def test_multiple_tool_calls_pair_in_order():
    msgs = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "t1", "arguments": {}}},
                {"function": {"name": "t2", "arguments": {}}},
            ],
        },
        {"role": "tool", "tool_name": "t1", "content": "r1"},
        {"role": "tool", "tool_name": "t2", "content": "r2"},
    ]
    out = llamacpp._to_openai_messages(msgs)
    id1, id2 = out[0]["tool_calls"][0]["id"], out[0]["tool_calls"][1]["id"]
    assert out[1]["tool_call_id"] == id1
    assert out[2]["tool_call_id"] == id2
    assert id1 != id2


def test_none_arguments_become_empty_object():
    msgs = [{"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "t", "arguments": None}}]}]
    out = llamacpp._to_openai_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"


# ---------------------------------------------------------------------------
# _parse_tool_calls  (OpenAI response -> ollama shape)
# ---------------------------------------------------------------------------

def test_parse_tool_calls_string_args_to_dict():
    msg = {
        "tool_calls": [
            {"function": {"name": "get_spot", "arguments": '{"spot_id": "x"}'}},
        ]
    }
    calls = llamacpp._parse_tool_calls(msg)
    assert calls == [{"function": {"name": "get_spot", "arguments": {"spot_id": "x"}}}]


def test_parse_tool_calls_bad_json_args_to_empty():
    msg = {"tool_calls": [{"function": {"name": "t", "arguments": "not json"}}]}
    assert llamacpp._parse_tool_calls(msg) == [{"function": {"name": "t", "arguments": {}}}]


def test_parse_tool_calls_empty():
    assert llamacpp._parse_tool_calls({}) == []


# ---------------------------------------------------------------------------
# Fake httpx.AsyncClient
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeStreamResp:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeClient:
    """Captures the last posted payload and returns a scripted response."""
    last_payload = None

    def __init__(self, json_payload=None, stream_lines=None, **kwargs):
        self._json_payload = json_payload
        self._stream_lines = stream_lines or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.last_payload = json
        return _FakeResp(self._json_payload)

    @asynccontextmanager
    async def stream(self, method, url, json=None):
        _FakeClient.last_payload = json
        yield _FakeStreamResp(self._stream_lines)


def _client_factory(**resp_kwargs):
    def _make(**kwargs):
        return _FakeClient(**resp_kwargs)
    return _make


# ---------------------------------------------------------------------------
# chat()  — non-streaming planning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_maps_tool_calls_back_to_ollama_shape():
    payload = {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{"function": {"name": "get_spot", "arguments": '{"spot_id": "y"}'}}],
            }
        }]
    }
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(json_payload=payload)):
        result = await llamacpp.chat(
            [{"role": "user", "content": "hi"}],
            base_url="http://x", tools=[{"type": "function"}], temperature=0.0,
        )
    assert result["content"] == ""
    assert result["tool_calls"] == [{"function": {"name": "get_spot", "arguments": {"spot_id": "y"}}}]
    # tools passed through unchanged; stream disabled
    assert _FakeClient.last_payload["tools"] == [{"type": "function"}]
    assert _FakeClient.last_payload["stream"] is False


@pytest.mark.asyncio
async def test_chat_no_tool_calls_omits_key():
    payload = {"choices": [{"message": {"content": "just text"}}]}
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(json_payload=payload)):
        result = await llamacpp.chat([{"role": "user", "content": "hi"}], base_url="http://x")
    assert result == {"content": "just text"}


# ---------------------------------------------------------------------------
# complete()  — utility JSON / prose
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_with_schema_sets_response_format():
    payload = {"choices": [{"message": {"content": '{"a": 1}'}}]}
    schema = {"type": "object"}
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(json_payload=payload)):
        raw = await llamacpp.complete("prompt", base_url="http://x", schema=schema)
    assert raw == '{"a": 1}'
    rf = _FakeClient.last_payload["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] is schema
    assert _FakeClient.last_payload["messages"] == [{"role": "user", "content": "prompt"}]


@pytest.mark.asyncio
async def test_complete_without_schema_is_plain_chat():
    payload = {"choices": [{"message": {"content": "prose"}}]}
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(json_payload=payload)):
        raw = await llamacpp.complete("p", base_url="http://x", temperature=0.7)
    assert raw == "prose"
    assert "response_format" not in _FakeClient.last_payload
    assert _FakeClient.last_payload["temperature"] == 0.7


# ---------------------------------------------------------------------------
# stream_chat()  — streaming generation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_chat_yields_content_then_sentinel_and_excludes_reasoning():
    lines = [
        'data: {"choices":[{"delta":{"reasoning_content":"thinking..."}}]}',  # dropped
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        "",  # blank line ignored
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        "data: [DONE]",
    ]
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(stream_lines=lines)):
        out = [item async for item in llamacpp.stream_chat(
            [{"role": "user", "content": "hi"}], base_url="http://x")]
    assert out[:2] == ["Hel", "lo"]
    # No tools passed -> tool_calls empty; content-only stream ends "stop"/None.
    assert out[-1] == {"_done": True, "token_count": 2, "tool_calls": [], "finish_reason": None}


@pytest.mark.asyncio
async def test_stream_chat_sentinel_without_explicit_done():
    lines = ['data: {"choices":[{"delta":{"content":"x"}}]}']
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(stream_lines=lines)):
        out = [item async for item in llamacpp.stream_chat(
            [{"role": "user", "content": "hi"}], base_url="http://x")]
    assert out == ["x", {"_done": True, "token_count": 1, "tool_calls": [], "finish_reason": None}]


@pytest.mark.asyncio
async def test_stream_chat_accumulates_tool_call_fragments():
    # Phase 0e E3: a single tool call surfaces across many delta.tool_calls
    # fragments (id/name once, arguments streamed as string pieces), ending
    # finish_reason=tool_calls, with ZERO content tokens on a tool hop (Phase 0).
    def line(obj):
        return "data: " + json.dumps(obj)

    lines = [
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_spot", "arguments": ""}}]}}]}),
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"spot_id": '}}]}}]}),
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '"abc"}'}}]}}]}),
        line({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        "data: [DONE]",
    ]
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(stream_lines=lines)):
        out = [item async for item in llamacpp.stream_chat(
            [{"role": "user", "content": "hi"}], base_url="http://x",
            tools=[{"type": "function"}])]
    # No content tokens streamed on a tool hop.
    assert out == [{
        "_done": True,
        "token_count": 0,
        "tool_calls": [
            {"id": "call_1", "function": {"name": "get_spot", "arguments": {"spot_id": "abc"}}}
        ],
        "finish_reason": "tool_calls",
    }]
    # tools were forwarded in the request payload.
    assert _FakeClient.last_payload["tools"] == [{"type": "function"}]


@pytest.mark.asyncio
async def test_stream_chat_assembles_multiple_calls_by_index():
    def line(obj):
        return "data: " + json.dumps(obj)

    lines = [
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "t0", "arguments": "{}"}}]}}]}),
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "b", "function": {"name": "t1", "arguments": "{}"}}]}}]}),
        "data: [DONE]",
    ]
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(stream_lines=lines)):
        out = [item async for item in llamacpp.stream_chat(
            [{"role": "user", "content": "hi"}], base_url="http://x", tools=[{}])]
    calls = out[-1]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == ["t0", "t1"]  # ordered by index


@pytest.mark.asyncio
async def test_stream_chat_bad_json_tool_args_kept_as_string():
    # Malformed/incomplete arguments fall back to the raw string; _coerce_args
    # downstream tolerates it (parses or -> {}), so the loop never crashes.
    def line(obj):
        return "data: " + json.dumps(obj)

    lines = [
        line({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "t", "arguments": "not json"}}]}}]}),
        "data: [DONE]",
    ]
    with patch.object(llamacpp.httpx, "AsyncClient", _client_factory(stream_lines=lines)):
        out = [item async for item in llamacpp.stream_chat(
            [{"role": "user", "content": "hi"}], base_url="http://x", tools=[{}])]
    assert out[-1]["tool_calls"] == [
        {"id": "a", "function": {"name": "t", "arguments": "not json"}}
    ]
