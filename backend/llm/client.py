"""
Ollama LLM client + faster-whisper transcription.

RESIDENT_MODELS are kept loaded at all times (keep_alive=-1).
The vision model uses keep_alive=0 — evicted immediately after response
to avoid displacing the resident 8B model from RAM (§6.1).

call_json_llm() is the single entry point for all prompts that require
structured JSON output.  Prose-generating prompts (map spatial description,
debrief summarisation) call ollama_generate() directly.

transcribe_audio() uses faster-whisper (whisper-small, CPU int8) for
server-side voice transcription.  The model is loaded once and cached.
"""

import json
import logging
import os
import tempfile

import httpx
from faster_whisper import WhisperModel

from config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# faster-whisper (voice transcription)
# ---------------------------------------------------------------------------

_whisper_model: WhisperModel | None = None


def _get_whisper() -> WhisperModel:
    global _whisper_model
    if _whisper_model is None:
        log.info("whisper_load_start", extra={"model": "small"})
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        log.info("whisper_load_done")
    return _whisper_model


def transcribe_audio(audio_bytes: bytes) -> str:
    """
    Transcribe audio bytes using faster-whisper (whisper-small, CPU int8).

    Accepts any format ffmpeg can decode (webm, ogg, mp4, wav).
    The model singleton is loaded on first call and reused for all subsequent calls.
    Runs synchronously — call via run_in_executor from async contexts.
    """
    model = _get_whisper()
    fd, tmp_path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio_bytes)
        segments, _ = model.transcribe(tmp_path, beam_size=5)
        return " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

RESIDENT_MODELS = {"llama3.1:8b-instruct-q4_K_M", "nomic-embed-text"}
CHAT_MODEL = "llama3.1:8b-instruct-q4_K_M"
VISION_MODEL = "llama3.2-vision:11b-instruct-q4_K_M"
EMBED_MODEL = "nomic-embed-text"

_GENERATE_URL = "/api/generate"
_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=10.0, pool=5.0)


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

    Uses streaming mode so the httpx read timeout resets per token chunk rather
    than waiting for the entire response — essential for long vision model outputs
    (OCR, spatial descriptions) on CPU-only hardware where generation takes minutes.

    keep_alive=-1 keeps resident models loaded indefinitely.
    keep_alive=0 evicts the model immediately after the response (vision only).
    """
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": temperature},
        "keep_alive": keep_alive,
    }
    if format:
        payload["format"] = format
    if images:
        payload["images"] = images

    parts: list[str] = []
    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=_TIMEOUT) as client:
        async with client.stream("POST", _GENERATE_URL, json=payload) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                parts.append(chunk.get("response", ""))
                if chunk.get("done"):
                    break
    return "".join(parts)


async def call_json_llm(
    prompt: str,
    model: str,
    default: dict,
    images: list[str] | None = None,
    keep_alive: int | None = None,
) -> dict:
    """
    Call the LLM expecting a JSON response.  Retries once with a correction
    nudge before returning the safe default.

    temperature=0.0 and format="json" are always used for JSON calls.
    keep_alive: overrides the default resident-model policy when provided.
      Default policy: -1 for resident models (never evict), 0 for others (evict immediately).
      Pass keep_alive=60 for intermediate vision calls so the model stays warm across
      consecutive calls in the same ingestion pipeline run.
    images: list of base64-encoded image strings (required for vision model calls).
    """
    if keep_alive is None:
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
