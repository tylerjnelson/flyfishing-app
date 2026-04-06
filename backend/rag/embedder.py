"""
Text embedder — nomic-embed-text via Ollama.

embed_text() is the single entry point for generating 768-dimensional
embeddings. Used for spot name_embedding generation and note vector indexing.

Raises httpx.HTTPError on Ollama communication failure — callers are
responsible for circuit breaking or retry as appropriate to their context.
"""

import logging

import httpx

from config import settings

log = logging.getLogger(__name__)

_EMBED_URL = "/api/embeddings"
_EMBED_MODEL = "nomic-embed-text"
_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=5.0, pool=5.0)


async def embed_text(text: str) -> list[float]:
    """
    Generate a 768-dimensional embedding for the given text.

    Returns a list of floats. Raises httpx.HTTPError if Ollama is unavailable.
    """
    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=_TIMEOUT) as client:
        resp = await client.post(
            _EMBED_URL,
            json={"model": _EMBED_MODEL, "prompt": text},
        )
        resp.raise_for_status()
        embedding = resp.json()["embedding"]

    log.debug("embedded_text", extra={"chars": len(text), "dims": len(embedding)})
    return embedding
