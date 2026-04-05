"""
All LLM prompt templates, versioned alongside the schema.
Model and prompt version are logged with each LLM call.
Templates use {placeholder} syntax.

Prompts that require JSON output are routed through call_json_llm().
Prose-generating prompts (§18.4, §18.5) call ollama_generate() directly.
"""

# ---------------------------------------------------------------------------
# §18.7  WTA fishing-intent classifier
# ---------------------------------------------------------------------------

WTA_FISHING_INTENT_PROMPT = """
Determine whether the following WTA trip report describes active fly fishing
or fishing as a primary or secondary activity.

Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

{{
  "fishing_intent": true | false,
  "confidence": "high" | "medium" | "low",
  "evidence": ""  // one sentence quoting or summarising the fishing signal,
                  // or 'none' if fishing_intent is false
}}

fishing_intent = true when the report:
- Explicitly mentions fishing, fly fishing, casting, catching fish, or fishing gear
- Describes fish species encountered in a fishing context (not wildlife observation)
- Recommends the location for fishing

fishing_intent = false when the report:
- Only mentions water features incidentally (e.g. 'crossed a stream')
- Mentions fish as wildlife without a fishing context
- Is purely a hiking, camping, or scrambling report with no fishing reference

confidence reflects how clearly the report signals fishing intent.
When fishing_intent is false, confidence describes certainty of the negative.

# Downstream usage:
# fishing_intent=false → report discarded, no spot extracted
# fishing_intent=true, confidence=high|medium → proceed to location extraction
# fishing_intent=true, confidence=low → proceed with seed_confidence='unvalidated'

Trip report text:
{report_text}
"""

# ---------------------------------------------------------------------------
# Additional prompts added in later phases:
#   §18.1  RECOMMENDATION_SYSTEM_PROMPT       (Phase 5 — context_builder.py)
#   §18.2  FIELD_EXTRACTION_PROMPT            (Phase 4 — ingestion.py)
#   §18.3  MAP_DETECTION_PROMPT               (Phase 4 — map_extractor.py)
#   §18.4  MAP_SPATIAL_DESCRIPTION_PROMPT     (Phase 4 — map_extractor.py)
#   §18.5  DEBRIEF_SUMMARISATION_PROMPT       (Phase 6 — trips/service.py)
#   §18.6  LOCATION_EXTRACTION_PROMPT         (Phase 4 — spot_resolver.py)
#   §18.8  WDFW_REGS_PARSER_PROMPT            (Phase 3 — wdfw_regulations.py)
# ---------------------------------------------------------------------------
