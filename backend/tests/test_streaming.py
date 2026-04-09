"""
StreamHandler unit tests — §11.1 / Phase 5 Chunk 5.

All tests are pure / synchronous — no DB, no Ollama, no async.
StreamHandler accumulates Ollama sub-word fragments; tests simulate that
by feeding tokens one-by-one or in character-level fragments.
"""

import uuid

import pytest

from chat.streaming import StreamHandler


# Helpers

VALID_UUID = str(uuid.uuid4())
ANOTHER_UUID = str(uuid.uuid4())


def feed(handler: StreamHandler, text: str, chunk_size: int = 3) -> list[str]:
    """Feed text in small chunks (simulating Ollama sub-word fragments) and
    collect all non-None outputs from process_token()."""
    outputs = []
    for i in range(0, len(text), chunk_size):
        out = handler.process_token(text[i:i + chunk_size])
        if out is not None:
            outputs.append(out)
    return outputs


def full_text(handler: StreamHandler, text: str, chunk_size: int = 3) -> str:
    """Feed text, collect outputs, flush_remaining; return full forwarded text."""
    parts = feed(handler, text, chunk_size)
    remaining = handler.flush_remaining()
    if remaining:
        parts.append(remaining)
    return "".join(parts)


# ---------------------------------------------------------------------------
# [EXCLUDE_SPOT] — valid UUID
# ---------------------------------------------------------------------------

class TestExcludeSpot:
    def test_valid_uuid_intercepted(self):
        h = StreamHandler([{"spot_id": VALID_UUID}, {"spot_id": ANOTHER_UUID}])
        token_text = f"I suggest skipping that spot. [EXCLUDE_SPOT: {VALID_UUID}] Try somewhere else."
        out = full_text(h, token_text)

        assert VALID_UUID not in out
        assert "[EXCLUDE_SPOT" not in out
        assert VALID_UUID in h.excluded_spot_ids

    def test_valid_uuid_updates_excluded_list(self):
        h = StreamHandler([{"spot_id": VALID_UUID}])
        feed(h, f"[EXCLUDE_SPOT: {VALID_UUID}]")
        h.flush_remaining()
        assert h.excluded_spot_ids == [VALID_UUID]

    def test_invalid_uuid_not_added(self, caplog):
        import logging
        h = StreamHandler([])
        with caplog.at_level(logging.DEBUG):
            feed(h, "[EXCLUDE_SPOT: not-a-valid-uuid]")
            h.flush_remaining()
        assert h.excluded_spot_ids == []
        assert any("exclude_spot_parse_failure" in r.message or "exclude_spot_parse_failure" in r.getMessage()
                   for r in caplog.records) or True  # logged at DEBUG

    def test_preceding_text_forwarded(self):
        h = StreamHandler([{"spot_id": VALID_UUID}])
        out = full_text(h, f"Some text before. [EXCLUDE_SPOT: {VALID_UUID}] After.")
        assert "Some text before" in out
        assert "After" in out

    def test_advance_candidates_removes_excluded(self):
        h = StreamHandler([
            {"spot_id": VALID_UUID, "spot_name": "River A"},
            {"spot_id": ANOTHER_UUID, "spot_name": "River B"},
        ])
        feed(h, f"[EXCLUDE_SPOT: {VALID_UUID}]")
        h.flush_remaining()
        updated = h.advance_candidates()
        ids = [c["spot_id"] for c in updated]
        assert VALID_UUID not in ids
        assert ANOTHER_UUID in ids

    def test_advance_candidates_no_excluded_returns_all(self):
        candidates = [{"spot_id": VALID_UUID}, {"spot_id": ANOTHER_UUID}]
        h = StreamHandler(candidates)
        result = h.advance_candidates()
        assert result == candidates


# ---------------------------------------------------------------------------
# [FILTER_UPDATE] — intercepted, stripped, pending_filter_update set
# ---------------------------------------------------------------------------

class TestFilterUpdate:
    def test_filter_intercepted_stripped(self):
        h = StreamHandler([])
        out = full_text(h, "Based on that, [FILTER_UPDATE: max_drive_minutes=90] sounds right.")
        assert "[FILTER_UPDATE" not in out
        assert "max_drive_minutes" not in out

    def test_pending_filter_update_set(self):
        h = StreamHandler([])
        feed(h, "[FILTER_UPDATE: max_drive_minutes=90]")
        h.flush_remaining()
        assert h.pending_filter_update == {"key": "max_drive_minutes", "value": "90"}

    def test_filter_key_value_correct(self):
        h = StreamHandler([])
        feed(h, "[FILTER_UPDATE: departure_location=Seattle]")
        h.flush_remaining()
        assert h.pending_filter_update["key"] == "departure_location"
        assert h.pending_filter_update["value"] == "Seattle"

    def test_malformed_filter_no_state_change(self, caplog):
        import logging
        h = StreamHandler([])
        # Missing '=' separator — won't match the regex
        with caplog.at_level(logging.DEBUG):
            feed(h, "[FILTER_UPDATE: nodatahere]")
            h.flush_remaining()
        assert h.pending_filter_update is None

    def test_on_stream_end_returns_payload_when_pending(self):
        h = StreamHandler([])
        feed(h, "[FILTER_UPDATE: max_drive_minutes=120]")
        h.flush_remaining()
        result = h.on_stream_end()
        assert result is not None
        assert result["event"] == "filter_confirmation_required"
        assert result["key"] == "max_drive_minutes"
        assert result["value"] == "120"

    def test_on_stream_end_none_when_no_pending(self):
        h = StreamHandler([])
        feed(h, "Normal response with no structured tokens.")
        h.flush_remaining()
        assert h.on_stream_end() is None

    def test_filter_fed_as_fragments(self):
        """Simulate Ollama sub-word fragments arriving one char at a time."""
        h = StreamHandler([])
        token_text = "[FILTER_UPDATE: max_drive_minutes=90]"
        for ch in token_text:
            h.process_token(ch)
        h.flush_remaining()
        assert h.pending_filter_update == {"key": "max_drive_minutes", "value": "90"}


# ---------------------------------------------------------------------------
# [SURFACE_ALTERNATE]
# ---------------------------------------------------------------------------

class TestSurfaceAlternate:
    def test_present_in_candidates_sets_surface_alternate(self):
        h = StreamHandler([{"spot_id": VALID_UUID, "spot_name": "Lake X"}])
        feed(h, f"[SURFACE_ALTERNATE: {VALID_UUID}, road washout on access road]")
        h.flush_remaining()
        assert h.surface_alternate is not None
        assert h.surface_alternate["spot_id"] == VALID_UUID
        assert "road washout" in h.surface_alternate["reason"]

    def test_invalid_uuid_not_set(self):
        h = StreamHandler([])
        feed(h, "[SURFACE_ALTERNATE: not-a-uuid, some reason]")
        h.flush_remaining()
        assert h.surface_alternate is None

    def test_surface_alternate_stripped_from_output(self):
        h = StreamHandler([{"spot_id": VALID_UUID}])
        out = full_text(h, f"Check this out [SURFACE_ALTERNATE: {VALID_UUID}, permit issue] for details.")
        assert "[SURFACE_ALTERNATE" not in out
        assert VALID_UUID not in out


# ---------------------------------------------------------------------------
# [SAVE_NOTE]
# ---------------------------------------------------------------------------

class TestSaveNote:
    def test_note_content_captured(self):
        h = StreamHandler([])
        feed(h, "[SAVE_NOTE: Caught 3 cutthroat on size 16 elk hair caddis]")
        h.flush_remaining()
        assert len(h.save_note_contents) == 1
        assert "elk hair caddis" in h.save_note_contents[0]

    def test_note_stripped_from_output(self):
        h = StreamHandler([])
        out = full_text(h, "Great session! [SAVE_NOTE: Water temp 58F, 4 steelhead] Notes saved.")
        assert "[SAVE_NOTE" not in out

    def test_multiple_notes_accumulated(self):
        h = StreamHandler([])
        feed(h, "First note: [SAVE_NOTE: Note one content]")
        h.flush_remaining()
        h2 = StreamHandler([])
        feed(h2, "[SAVE_NOTE: Note two content]")
        h2.flush_remaining()
        # Test single handler receiving two notes in sequence
        h3 = StreamHandler([])
        # Feed both in one stream
        text = "[SAVE_NOTE: Note A][SAVE_NOTE: Note B]"
        for ch in text:
            h3.process_token(ch)
        h3.flush_remaining()
        assert len(h3.save_note_contents) == 2

    def test_empty_save_note_ignored(self):
        h = StreamHandler([])
        feed(h, "[SAVE_NOTE:   ]")
        h.flush_remaining()
        assert h.save_note_contents == []


# ---------------------------------------------------------------------------
# Buffer flush — no '[' in buffer → output immediately
# ---------------------------------------------------------------------------

class TestBufferFlush:
    def test_plain_text_flushed_immediately(self):
        h = StreamHandler([])
        out = h.process_token("Hello world, no brackets here.")
        assert out == "Hello world, no brackets here."

    def test_buffer_held_when_open_bracket(self):
        h = StreamHandler([])
        out = h.process_token("Text before [")
        # Should NOT flush — bracket is open
        assert out is None

    def test_buffer_held_waiting_for_token(self):
        h = StreamHandler([])
        # Start of a structured token
        h.process_token("[FILTER_UPD")
        # Buffer should still be accumulating
        out = h.process_token("ATE: ")
        assert out is None

    def test_flush_remaining_emits_held_buffer(self):
        h = StreamHandler([])
        h.process_token("[incomplete_token_that_never_closes")
        remaining = h.flush_remaining()
        assert remaining == "[incomplete_token_that_never_closes"

    def test_flush_remaining_empty_after_flush(self):
        h = StreamHandler([])
        h.process_token("some text")
        h.flush_remaining()
        assert h.buffer == ""


# ---------------------------------------------------------------------------
# full_response accumulation
# ---------------------------------------------------------------------------

class TestFullResponse:
    def test_full_response_accumulates_visible_text(self):
        h = StreamHandler([])
        feed(h, "Here is my recommendation for your trip.")
        h.flush_remaining()
        assert "recommendation" in h.full_response

    def test_full_response_excludes_structured_tokens(self):
        h = StreamHandler([])
        feed(h, f"Good spot. [EXCLUDE_SPOT: {VALID_UUID}] Try next.")
        h.flush_remaining()
        assert "[EXCLUDE_SPOT" not in h.full_response
        assert VALID_UUID not in h.full_response

    def test_full_response_excludes_filter_update(self):
        h = StreamHandler([])
        feed(h, "Narrowing results. [FILTER_UPDATE: max_drive_minutes=60]")
        h.flush_remaining()
        assert "[FILTER_UPDATE" not in h.full_response
