#!/usr/bin/env python3
"""
Static-prefix pre-warm for the llama-server chat instance (Phase 1, Piece 3).

Cold-prefills slot 0 with the *static* planning prefix that every turn's planning
pass shares — the compact PLANNING_SYSTEM_PROMPT + the tool catalog — so
llama-server's built-in prefix matching reuses those KV tokens on subsequent
requests instead of re-prefilling them on CPU (Phase 0d D7 / Phase 0e E4:
~45 s cold-open prefill collapses to a slot restore).

Under the current two-pass app the only genuinely static, shared prefix is the
planning head — the generation-pass system prompt still carries dynamic
conditions. Phase 4's frozen-context prefix will extend what is worth pre-warming;
this script deliberately warms just the planning head today. (Judgment call flagged
in the Phase 1 build — revisit the pre-warm target when Phase 4 lands.)

Run at deploy / boot AFTER the chat unit is up, e.g. via
deploy/flyfish-llama-prewarm.service. Requires the app env (EnvironmentFile
/etc/flyfish/app.env) because it imports the real prompt + tool definitions so the
warmed tokens are byte-identical to what run_tool_planning sends.

Idempotent and non-fatal: a failed save is logged, not raised — the prefill itself
is the win; the saved slot file only accelerates a subsequent restart.
"""

import asyncio
import logging
import os
import sys

import httpx

# Import the app's real static prefix so the warmed KV matches production requests.
# backend/ (for chat, llm) + repo root (for prompts/) — mirrors main.py's path setup.
_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _BACKEND)
sys.path.insert(0, os.path.abspath(os.path.join(_BACKEND, "..")))
from chat.tools import TOOL_SCHEMAS, _build_planning_messages  # noqa: E402
from llm import llamacpp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("prewarm_llama")

CHAT_URL = os.environ.get("FLYFISH_LLAMA_CHAT_URL", "http://127.0.0.1:8080")
SLOT_FILE = os.environ.get("FLYFISH_LLAMA_PREWARM_FILE", "base_planning_prefix.bin")
HEALTH_TIMEOUT_S = float(os.environ.get("FLYFISH_LLAMA_PREWARM_HEALTH_TIMEOUT", "240"))


async def _wait_for_health(client: httpx.AsyncClient) -> None:
    """Poll /health until the model has finished loading (200 OK)."""
    deadline = asyncio.get_event_loop().time() + HEALTH_TIMEOUT_S
    while True:
        try:
            resp = await client.get("/health")
            if resp.status_code == 200:
                log.info("chat instance healthy")
                return
        except httpx.HTTPError:
            pass
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"/health not green within {HEALTH_TIMEOUT_S:.0f}s")
        await asyncio.sleep(2.0)


async def main() -> int:
    # The static head: PLANNING_SYSTEM_PROMPT + the "(none in current context)"
    # spot block (empty candidates) + the tool catalog. Real requests share this
    # up to where the concrete spot list diverges — the bulk of the prefill.
    messages = _build_planning_messages([], [])
    payload = {
        "model": "gemma",
        "messages": llamacpp._to_openai_messages(messages),
        "tools": TOOL_SCHEMAS,
        "stream": False,
        "temperature": 0.0,
        "max_tokens": 1,
    }

    timeout = httpx.Timeout(connect=5.0, read=HEALTH_TIMEOUT_S, write=10.0, pool=5.0)
    async with httpx.AsyncClient(base_url=CHAT_URL, timeout=timeout) as client:
        await _wait_for_health(client)

        log.info("prefilling static planning prefix (%d tools)", len(TOOL_SCHEMAS))
        resp = await client.post("/v1/chat/completions", json=payload)
        resp.raise_for_status()
        usage = resp.json().get("usage", {}) or {}
        log.info("prefill done — prompt_tokens=%s", usage.get("prompt_tokens", "?"))

        # Persist slot 0 so a service restart can restore the prefix instead of
        # cold-prefilling again. Requires --slot-save-path on the chat unit.
        try:
            save = await client.post(f"/slots/0?action=save&filename={SLOT_FILE}")
            save.raise_for_status()
            log.info("saved slot 0 -> %s", SLOT_FILE)
        except httpx.HTTPError as exc:
            log.warning("slot save failed (non-fatal): %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
