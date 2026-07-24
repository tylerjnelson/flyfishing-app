"""
llama.cpp `llama-server` adapter (Phase 1 — Option B engine switchover).

Active only when ``settings.chat_engine == "llamacpp"``. Translates the app's
ollama-shaped chat calls to llama-server's OpenAI ``/v1/chat/completions``
endpoint (server launched with ``--jinja`` so the canonical gemma chat template
is applied). Two weight-sharing instances, selected by ``base_url``:

  - **chat**    (``settings.llama_chat_url``, launched ``--reasoning on``):
    interactive planning (non-streaming, native tool calling) + streaming
    generation.
  - **utility** (``settings.llama_util_url``, launched ``--reasoning off``):
    the three background gemma tasks — field extraction, location extraction,
    debrief summarisation.

Both are the *same endpoint shape* (one code path). Phase 0g (2026-07-24)
rejected the raw ``/completion`` design: ``/completion`` sends the prompt
untemplated (unlike ollama ``/api/generate``, which applies the gemma template),
which made the debrief task hallucinate fabricated dialogue turns. The templated
chat endpoint fixes it and byte-matches ollama.

Reasoning ("thinking") arrives as ``choices[].message.reasoning_content`` (or
``delta.reasoning_content`` when streaming) and is **never** forwarded — this
mirrors the way the ollama path drops the ``thinking`` field (Phase 0f: zero
leak). Because we only ever read ``content``, reasoning is excluded structurally.

See dev-instructions/build-log/agentic-harness-plan.md — Phase 1 Pieces 1 & 5.
"""

import json
import logging

import httpx

log = logging.getLogger(__name__)

_CHAT_COMPLETIONS_URL = "/v1/chat/completions"

# llama-server serves a single loaded model; the `model` field is required by the
# OpenAI schema but its value is ignored when only one model is loaded.
_MODEL = "gemma"

# Mirror the ollama client timeouts: long read window for CPU-bound generation,
# short connect. The streaming variant resets its read timeout per token chunk.
_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=10.0, pool=5.0)
_STREAM_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


# ---------------------------------------------------------------------------
# Message normalisation (ollama shape -> OpenAI shape)
# ---------------------------------------------------------------------------

def _to_openai_messages(messages: list[dict]) -> list[dict]:
    """
    Convert the app's ollama-shaped messages to OpenAI chat format so the gemma
    ``--jinja`` template renders them correctly.

    The only non-trivial cases are the planning tool round-trip (Phase 5):
      - assistant tool-call messages carry ``tool_calls[].function.arguments`` as
        a **dict** (ollama) -> OpenAI wants a JSON **string**, plus a stable
        ``id`` and ``type: "function"``.
      - tool-result messages use ``tool_name`` (ollama) -> OpenAI pairs them to
        the call via ``tool_call_id``. We pair by order (the planner emits the
        assistant call message immediately before its results, in the same
        order), carrying a FIFO of the ids we just minted.

    Plain system/user/assistant messages pass through unchanged.
    """
    out: list[dict] = []
    pending_ids: list[str] = []  # tool_call ids awaiting their result message

    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            tcs: list[dict] = []
            for j, tc in enumerate(m["tool_calls"]):
                fn = tc.get("function", {}) or {}
                cid = tc.get("id") or f"call_{idx}_{j}"
                args = fn.get("arguments")
                if isinstance(args, (dict, list)):
                    args = json.dumps(args)
                elif args is None:
                    args = "{}"
                tcs.append({
                    "id": cid,
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args},
                })
                pending_ids.append(cid)
            out.append({
                "role": "assistant",
                "content": m.get("content", "") or "",
                "tool_calls": tcs,
            })
        elif role == "tool":
            cid = m.get("tool_call_id") or (pending_ids.pop(0) if pending_ids else None)
            entry: dict = {"role": "tool", "content": m.get("content", "") or ""}
            if cid:
                entry["tool_call_id"] = cid
            if m.get("tool_name"):
                entry["name"] = m["tool_name"]
            out.append(entry)
        else:
            out.append({"role": role, "content": m.get("content", "") or ""})

    return out


def _parse_tool_calls(msg: dict) -> list[dict]:
    """Map OpenAI ``message.tool_calls`` back to the ollama shape callers expect:
    ``[{"function": {"name": str, "arguments": dict}}]``."""
    calls: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append({"function": {"name": fn.get("name", ""), "arguments": args or {}}})
    return calls


def _assemble_streamed_tool_calls(acc: dict[int, dict]) -> list[dict]:
    """Fold accumulated streaming ``delta.tool_calls`` fragments into whole calls.

    OpenAI streams a tool call across many ``delta.tool_calls`` fragments keyed by
    ``index`` (Phase 0e E3 saw ~69 fragments for one call): ``id`` and
    ``function.name`` arrive once, ``function.arguments`` streams as string pieces
    that must be concatenated. Returns the ollama shape the loop dispatches on —
    ``[{"id": str|None, "function": {"name": str, "arguments": dict|str}}]`` — with
    ``arguments`` parsed to a dict when it forms valid JSON (``_coerce_args``
    downstream tolerates a leftover string).
    """
    calls: list[dict] = []
    for idx in sorted(acc):
        slot = acc[idx]
        name = slot.get("name") or ""
        if not name:
            continue
        raw = slot.get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except json.JSONDecodeError:
            args = raw
        calls.append({"id": slot.get("id"), "function": {"name": name, "arguments": args}})
    return calls


# ---------------------------------------------------------------------------
# Chat instance — planning (non-streaming, tool calling)
# ---------------------------------------------------------------------------

async def chat(
    messages: list[dict],
    *,
    base_url: str,
    tools: list[dict] | None = None,
    temperature: float = 0.0,
) -> dict:
    """
    Non-streaming ``/v1/chat/completions`` call. Returns the assistant message in
    the **ollama shape** ``{"content": str, "tool_calls": [...]}`` so
    ``run_tool_planning`` is unchanged (its ``_coerce_args`` already accepts the
    dict arguments we hand back).

    ``tools`` (``chat.tools.TOOL_SCHEMAS``) is the OpenAI function-tool format,
    which llama-server accepts as-is — no translation.
    """
    payload: dict = {
        "model": _MODEL,
        "messages": _to_openai_messages(messages),
        "stream": False,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(base_url=base_url, timeout=_TIMEOUT) as client:
        resp = await client.post(_CHAT_COMPLETIONS_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    result: dict = {"content": msg.get("content", "") or ""}
    tool_calls = _parse_tool_calls(msg)
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


# ---------------------------------------------------------------------------
# Chat instance — generation (streaming)
# ---------------------------------------------------------------------------

async def stream_chat(
    messages: list[dict],
    *,
    base_url: str,
    tools: list[dict] | None = None,
    temperature: float = 0.7,
):
    """
    Yield raw token strings from streaming ``/v1/chat/completions``, then a final
    ``{"_done": True, "token_count": n, "tool_calls": [...], "finish_reason": str}``
    sentinel — matching ``_stream_ollama``'s contract (the two-pass generation path
    passes no ``tools`` and ignores the extra sentinel keys, so it is unchanged).

    When ``tools`` are supplied (the Phase 3 agentic loop), streaming
    ``delta.tool_calls`` fragments are accumulated across the whole response and
    surfaced whole in the sentinel's ``tool_calls`` (ollama shape). A tool hop
    streams zero ``content`` (Phase 0), so no user-facing token leaks before the
    call is dispatched.

    Only ``delta.content`` is forwarded; ``delta.reasoning_content`` (the thinking
    stream, present with ``--reasoning on``) is never yielded.
    """
    payload: dict = {
        "model": _MODEL,
        "messages": _to_openai_messages(messages),
        "stream": True,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools

    token_count = 0
    tool_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
    finish_reason: str | None = None

    def _sentinel() -> dict:
        return {
            "_done": True,
            "token_count": token_count,
            "tool_calls": _assemble_streamed_tool_calls(tool_acc),
            "finish_reason": finish_reason,
        }

    async with httpx.AsyncClient(base_url=base_url, timeout=_STREAM_TIMEOUT) as client:
        async with client.stream("POST", _CHAT_COMPLETIONS_URL, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    yield _sentinel()
                    return
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = (chunk.get("choices") or [{}])[0]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta", {}) or {}
                for frag in delta.get("tool_calls") or []:
                    idx = frag.get("index", 0)
                    slot = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                    if frag.get("id"):
                        slot["id"] = frag["id"]
                    fn = frag.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                token = delta.get("content") or ""
                if token:
                    token_count += 1
                    yield token
    # Stream ended without an explicit [DONE] — still emit the sentinel.
    yield _sentinel()


# ---------------------------------------------------------------------------
# Utility instance — one-shot completions (field/location JSON + debrief prose)
# ---------------------------------------------------------------------------

async def complete(
    prompt: str,
    *,
    base_url: str,
    temperature: float = 0.0,
    schema: dict | None = None,
) -> str:
    """
    Single-turn ``/v1/chat/completions`` call for the background utility tasks.
    The prompt is sent as one user message so ``--jinja`` wraps it in the gemma
    instruction template (the fix for the raw-``/completion`` hallucination — see
    module docstring / Phase 0g).

    ``schema`` (when given) is enforced via ``response_format`` json_schema —
    llama-server compiles the JSON Schema to GBNF internally (no hand-written
    grammar). Phase 0g: mandatory for the JSON tasks; unconstrained free-parse
    returned empty JSON on several notes. ``schema=None`` (debrief prose) sends a
    plain chat request.

    Returns the raw assistant ``content`` string; JSON parsing/retry stays in
    ``call_json_llm``.
    """
    payload: dict = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": temperature,
    }
    if schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": schema, "strict": True},
        }

    async with httpx.AsyncClient(base_url=base_url, timeout=_TIMEOUT) as client:
        resp = await client.post(_CHAT_COMPLETIONS_URL, json=payload)
        resp.raise_for_status()
        data = resp.json()

    msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
    return msg.get("content", "") or ""
