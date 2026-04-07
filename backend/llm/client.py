"""
Ollama LLM client.

RESIDENT_MODELS are kept loaded at all times (keep_alive=-1).
The vision model uses keep_alive=0 — evicted immediately after response
to avoid displacing the resident 8B model from RAM (§6.1).

call_json_llm() is the single entry point for all prompts that require
structured JSON output.  Prose-generating prompts (map spatial description,
debrief summarisation) call ollama_generate() directly.
"""

import json
import logging

import httpx

from config import settings

log = logging.getLogger(__name__)

RESIDENT_MODELS = {"llama3.1:8b-instruct-q4_K_M", "nomic-embed-text"}
CHAT_MODEL = "llama3.1:8b-instruct-q4_K_M"
VISION_MODEL = "llama3.2-vision:11b-instruct-q4_K_M"
EMBED_MODEL = "nomic-embed-text"

_GENERATE_URL = "/api/generate"
_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=10.0, pool=5.0)


async def ollama_generate(
    model: str,
    prompt: str,
    *,
    temperature: float = 0.7,
    format: str | None = None,
    keep_alive: int = -1,
    images: list[str] | None = None,
) -> str:
    """
    Send a generation request to Ollama and return the response string.

    keep_alive=-1 keeps resident models loaded indefinitely.
    keep_alive=0 evicts the model immediately after the response (vision only).
    """
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
        "keep_alive": keep_alive,
    }
    if format:
        payload["format"] = format
    if images:
        payload["images"] = images

    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=_TIMEOUT) as client:
        resp = await client.post(_GENERATE_URL, json=payload)
        resp.raise_for_status()
        return resp.json()["response"]


async def call_json_llm(
    prompt: str,
    model: str,
    default: dict,
    images: list[str] | None = None,
) -> dict:
    """
    Call the LLM expecting a JSON response.  Retries once with a correction
    nudge before returning the safe default.

    temperature=0.0 and format="json" are always used for JSON calls.
    keep_alive is set per the resident-model policy.
    images: list of base64-encoded image strings (required for vision model calls).
    """
    keep_alive = -1 if model in RESIDENT_MODELS else 0

    for attempt in range(2):
        raw = await ollama_generate(
            model,
            prompt,
            temperature=0.0,
            format="json",
            keep_alive=keep_alive,
            images=images,
        )
        cleaned = raw.strip().lstrip("```json").rstrip("```").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            if attempt == 0:
                prompt += (
                    "\n\nYour previous response was not valid JSON."
                    " Return ONLY the JSON object, starting with { and ending with }."
                )
                log.warning("json_parse_retry", extra={"model": model, "raw": raw[:200]})
            else:
                log.warning("json_parse_failed", extra={"model": model, "raw": raw[:200]})
                return default

    return default  # unreachable but satisfies type checkers
