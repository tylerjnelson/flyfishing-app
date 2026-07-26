"""
Streaming handler state machine — §6.5.

CRITICAL: Ollama streams sub-word fragments. Structured tokens like
[FILTER_UPDATE: max_drive_minutes=90] arrive as ~10 separate SSE events.
A per-token re.match() will NEVER see the complete pattern. Tokens must
accumulate in self.buffer; use re.search() on the full buffer.

StreamHandler is intentionally pure / synchronous — no DB access, no async.
The router drives the generator loop and applies DB writes after stream end
using the state this handler accumulates.
"""

import logging
import re

log = logging.getLogger(__name__)

# Structured token patterns
_RE_FILTER = re.compile(r'\[FILTER_UPDATE:\s*(\w+)\s*=\s*(.+?)\]')
_RE_SAVE_NOTE = re.compile(r'\[SAVE_NOTE:\s*(.+?)\]', re.DOTALL)
_RE_RECOMMEND = re.compile(r'\[RECOMMEND:\s*[^\]]+\]', re.IGNORECASE)


class StreamHandler:
    """
    Stateful buffer that processes Ollama token fragments and intercepts
    structured tokens before they reach the frontend SSE stream.

    Usage:
        handler = StreamHandler(session_candidates)
        for token in ollama_stream:
            text = handler.process_token(token)
            if text:
                yield sse_event("token", text)
        final = handler.on_stream_end()
        if final:
            yield sse_event("filter_confirmation_required", final)
    """

    def __init__(self, session_candidates: list[dict] = None):
        self.buffer: str = ""
        self.full_response: str = ""  # accumulates complete response for DB storage

        # Session state — applied to DB after stream ends
        self.pending_filter_update: dict | None = None
        self.save_note_contents: list[str] = []
        self.recommend_block: str | None = None  # raw "[RECOMMEND: ...]" for turn_builder

    # ------------------------------------------------------------------
    # Per-token processing
    # ------------------------------------------------------------------

    def process_token(self, token: str) -> str | None:
        """
        Append token to buffer and check for structured token patterns.

        Returns text to forward to the frontend SSE stream, or None to
        suppress (structured token consumed or buffer holding for more input).
        """
        self.buffer += token

        # --- [FILTER_UPDATE: key=value] ---
        m = re.search(_RE_FILTER, self.buffer)
        if m:
            key, value = m.group(1), m.group(2).strip()
            self.pending_filter_update = {"key": key, "value": value}
            log.debug("filter_update_intercepted", extra={"key": key, "value": value})
            pre = self.buffer[:m.start()]
            self.buffer = self.buffer[m.end():]
            out = pre if pre.strip() else None
            if out:
                self.full_response += out
            return out

        # --- [SAVE_NOTE: content] ---
        m = re.search(_RE_SAVE_NOTE, self.buffer)
        if m:
            note_content = m.group(1).strip()
            if note_content:
                self.save_note_contents.append(note_content)
                log.debug("save_note_intercepted", extra={"chars": len(note_content)})
            pre = self.buffer[:m.start()]
            self.buffer = self.buffer[m.end():]
            out = pre if pre.strip() else None
            if out:
                self.full_response += out
            return out

        # --- [RECOMMEND: uuid, uuid, uuid] ---
        m = re.search(_RE_RECOMMEND, self.buffer)
        if m:
            self.recommend_block = m.group(0)
            log.debug("recommend_block_intercepted", extra={"block": self.recommend_block})
            pre = self.buffer[:m.start()]
            self.buffer = self.buffer[m.end():]
            out = pre if pre.strip() else None
            if out:
                self.full_response += out
            return out

        # --- Buffer flush ---
        # Safe to emit buffered text when there is no open bracket that could
        # be the start of a structured token.
        if '[' not in self.buffer:
            out = self.buffer if self.buffer else None
            if out:
                self.full_response += out
            self.buffer = ""
            return out if out else None

        # Buffer contains '[' — hold and wait for more tokens
        return None

    # ------------------------------------------------------------------
    # Post-stream finalisation
    # ------------------------------------------------------------------

    def flush_remaining(self) -> str | None:
        """
        Flush any text remaining in the buffer after the stream ends.
        Called before on_stream_end() to emit trailing content.
        """
        if self.buffer:
            out = self.buffer
            self.full_response += out
            self.buffer = ""
            return out
        return None

    def on_stream_end(self) -> dict | None:
        """
        Return SSE payload for filter_confirmation_required, or None.

        Called after the token stream is exhausted. The router uses the
        returned dict to emit a final SSE event before closing the stream.
        """
        if self.pending_filter_update:
            return {
                "event": "filter_confirmation_required",
                "key": self.pending_filter_update["key"],
                "value": self.pending_filter_update["value"],
            }
        return None

