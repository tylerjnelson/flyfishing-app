"""
Phase 4 — ingestion unit tests.

Focus areas per §11.1:
  - negative_reason extraction prompt contract:
      null for positive/neutral outcomes
      closed enum enforcement (conditions | access | fish_absence | gear | unknown)
      a value outside the enum must be coerced to 'unknown', not stored raw

All tests are pure (no DB, no Ollama).
"""

import pytest

from notes.ingestion import _sanitise_fields


# ---------------------------------------------------------------------------
# _sanitise_fields — negative_reason enum enforcement
# ---------------------------------------------------------------------------


class TestSanitiseFields:
    """Test the _sanitise_fields() function that enforces enum contracts."""

    # --- negative_reason must be null for non-negative outcomes ---

    def test_positive_outcome_clears_negative_reason(self):
        fields = {
            "outcome": "positive",
            "negative_reason": "conditions",  # invalid — must be cleared
            "species": [],
            "flies": [],
            "approx_cfs": None,
            "approx_temp": None,
            "time_of_day": None,
        }
        result = _sanitise_fields(fields)
        assert result["negative_reason"] is None

    def test_neutral_outcome_clears_negative_reason(self):
        fields = {
            "outcome": "neutral",
            "negative_reason": "access",  # invalid — must be cleared
        }
        result = _sanitise_fields(fields)
        assert result["negative_reason"] is None

    def test_negative_outcome_preserves_valid_reason(self):
        for reason in ("conditions", "access", "fish_absence", "gear", "unknown"):
            fields = {"outcome": "negative", "negative_reason": reason}
            result = _sanitise_fields(fields)
            assert result["negative_reason"] == reason, (
                f"Expected {reason} to be preserved for negative outcome"
            )

    def test_negative_outcome_with_none_reason_coerced_to_unknown(self):
        """
        When outcome=negative and LLM returns null negative_reason, coerce to 'unknown'.
        The spec requires exactly one of the enum values for negative outcomes.
        """
        fields = {"outcome": "negative", "negative_reason": None}
        result = _sanitise_fields(fields)
        assert result["negative_reason"] == "unknown"

    # --- invalid enum values coerced to 'unknown' ---

    def test_out_of_enum_negative_reason_coerced_to_unknown(self):
        """
        A value outside the enum must NOT be stored raw.
        This would silently break scorer weighting (§11.1 warning).
        """
        invalid_values = [
            "weather",
            "too_crowded",
            "low_water",
            "UNKNOWN",   # wrong case
            "Conditions",  # wrong case
            "none",
            "",
            "random string",
        ]
        for bad_val in invalid_values:
            fields = {"outcome": "negative", "negative_reason": bad_val}
            result = _sanitise_fields(fields)
            assert result["negative_reason"] == "unknown", (
                f"Expected 'unknown' for bad value '{bad_val}', "
                f"got '{result['negative_reason']}'"
            )

    # --- outcome validation ---

    def test_invalid_outcome_coerced_to_neutral(self):
        for bad in ("great", "bad", "", None, "POSITIVE", "Neutral"):
            fields = {"outcome": bad, "negative_reason": "conditions"}
            result = _sanitise_fields(fields)
            assert result["outcome"] == "neutral", (
                f"Expected 'neutral' for bad outcome '{bad}'"
            )

    def test_valid_outcomes_preserved(self):
        for outcome in ("positive", "neutral", "negative"):
            fields = {"outcome": outcome, "negative_reason": None}
            result = _sanitise_fields(fields)
            assert result["outcome"] == outcome

    # --- time_of_day validation ---

    def test_invalid_time_of_day_set_to_none(self):
        for bad in ("dawn", "dusk", "night", "midday", "noon", "", "MORNING"):
            fields = {"outcome": "positive", "negative_reason": None, "time_of_day": bad}
            result = _sanitise_fields(fields)
            assert result["time_of_day"] is None, (
                f"Expected None for invalid time_of_day '{bad}'"
            )

    def test_valid_time_of_day_preserved(self):
        for tod in ("morning", "afternoon", "evening", "all-day"):
            fields = {"outcome": "positive", "negative_reason": None, "time_of_day": tod}
            result = _sanitise_fields(fields)
            assert result["time_of_day"] == tod

    # --- combined contract scenario: LLM returns garbage for negative outcome ---

    def test_full_garbage_response_is_safe(self):
        """
        Simulate an LLM response that violates multiple contracts at once.
        _sanitise_fields must make it safe to store.
        """
        raw = {
            "species": ["rainbow trout"],
            "flies": ["Adams"],
            "outcome": "bad",               # invalid outcome
            "negative_reason": "too windy",  # invalid reason + invalid outcome combo
            "approx_cfs": "high",           # should be numeric; left as-is (caller validates)
            "approx_temp": 52,
            "time_of_day": "dusk",          # invalid time_of_day
        }
        result = _sanitise_fields(raw)
        assert result["outcome"] == "neutral"           # coerced
        assert result["negative_reason"] is None        # cleared (outcome not "negative")
        assert result["time_of_day"] is None            # coerced


# ---------------------------------------------------------------------------
# Prompt contract tests — FIELD_EXTRACTION_PROMPT format substitution
# ---------------------------------------------------------------------------


class TestPromptContracts:
    """Verify that Phase 4 prompt templates format correctly without KeyError."""

    def test_field_extraction_prompt_formats(self):
        from prompts.registry import FIELD_EXTRACTION_PROMPT

        result = FIELD_EXTRACTION_PROMPT.format(note_text="Fished the Yakima today.")
        assert "Yakima" in result
        assert "{note_text}" not in result  # placeholder replaced

    def test_field_extraction_prompt_contains_enum_values(self):
        from prompts.registry import FIELD_EXTRACTION_PROMPT

        # Enum values must appear in the prompt so the LLM knows the contract
        for val in ("conditions", "access", "fish_absence", "gear", "unknown"):
            assert val in FIELD_EXTRACTION_PROMPT

    def test_location_extraction_prompt_formats(self):
        from prompts.registry import LOCATION_EXTRACTION_PROMPT

        result = LOCATION_EXTRACTION_PROMPT.format(note_text="Fished the Sky near Index.")
        assert "Index" in result
        assert "{note_text}" not in result

    def test_debrief_summary_prompt_formats(self):
        """DEBRIEF_SUMMARY_PROMPT uses .format(conversation_text=...)."""
        from prompts.registry import DEBRIEF_SUMMARY_PROMPT

        result = DEBRIEF_SUMMARY_PROMPT.format(conversation_text="Fished the Yakima. Caught two rainbows.")
        assert "Yakima" in result
        assert "{conversation_text}" not in result


# ---------------------------------------------------------------------------
# processing_notes helpers
# ---------------------------------------------------------------------------


class TestProcessingNotes:
    def test_build_flags_only(self):
        from notes.ingestion import _build_processing_notes

        result = _build_processing_notes(["awaiting_date_confirmation", "spot_auto_linked"], None)
        assert result == "awaiting_date_confirmation|spot_auto_linked"

    def test_build_with_json(self):
        import json

        from notes.ingestion import _build_processing_notes

        blob = json.dumps({"band": "medium", "candidates": []})
        result = _build_processing_notes(["awaiting_spot_confirmation"], blob)
        lines = result.split("\n", 1)
        assert lines[0] == "awaiting_spot_confirmation"
        assert json.loads(lines[1]) == {"band": "medium", "candidates": []}

    def test_empty_flags(self):
        from notes.ingestion import _build_processing_notes

        result = _build_processing_notes([], None)
        assert result == ""

    def test_parse_processing_notes_roundtrip(self):
        import json

        from notes.ingestion import _build_processing_notes
        from notes.service import parse_processing_notes

        blob = json.dumps({"band": "low", "location_string": "Sky"})
        raw = _build_processing_notes(["awaiting_spot_confirmation", "awaiting_date_confirmation"], blob)
        parsed = parse_processing_notes(raw)
        assert "awaiting_spot_confirmation" in parsed["flags"]
        assert "awaiting_date_confirmation" in parsed["flags"]
        assert parsed["spot_resolution"]["band"] == "low"
