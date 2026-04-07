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
# §18.2  Structured field extraction  (Llama 3.1 8B — call_json_llm)
# ---------------------------------------------------------------------------

FIELD_EXTRACTION_PROMPT = """
Extract structured fields from the fishing note text below.
Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

Schema:
{{
  "species": [],         // array of fish species mentioned
  "flies": [],           // array of fly patterns mentioned
  "outcome": "",         // exactly one of: positive | neutral | negative
  "negative_reason": null, // MUST be null unless outcome is negative.
                           // When negative, exactly one of:
                           // conditions | access | fish_absence | gear | unknown
                           // Do NOT invent values outside this list.
  "approx_cfs": null,    // integer if a flow reading is stated, else null
  "approx_temp": null,   // decimal if water temperature is stated, else null
  "time_of_day": null    // one of: morning | afternoon | evening | all-day | null
}}

Rules:
- negative_reason MUST be null when outcome is positive or neutral
- Return null for any field not explicitly stated in the note — do not infer
- If outcome is genuinely ambiguous, use neutral
- species and flies are arrays; return [] if none mentioned

Note text:
{note_text}
"""

# ---------------------------------------------------------------------------
# §18.3  Map detection  (Llama 3.2 11B Vision — call_json_llm with image)
# Used as-is (no .format() substitution needed — no placeholders).
# ---------------------------------------------------------------------------

MAP_DETECTION_PROMPT = """
Examine this notebook page image.
Determine whether it contains a hand-drawn map, diagram, or spatial sketch of a
fishing location — for example: a river with pools or runs marked, a lake with
access points, a trail route sketch, or annotated terrain features.

Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

{
  "contains_map": true | false,
  "confidence": "high" | "medium" | "low",
  "bounding_box": {
    "x": 0.0,
    "y": 0.0,
    "w": 1.0,
    "h": 1.0
  } | null
}

Set bounding_box to null if and only if contains_map is false.
If confidence is low, still provide your best bounding_box estimate.
"""

# ---------------------------------------------------------------------------
# §18.4  Map spatial description  (Llama 3.2 11B Vision — ollama_generate,
# NOT call_json_llm. Returns prose, not JSON.)
# Used as-is (no .format() substitution needed — no placeholders).
# ---------------------------------------------------------------------------

MAP_DESCRIPTION_PROMPT = """
Describe this hand-drawn fishing map in structured detail for a fly fishing
knowledge base. The map was drawn by an experienced angler and contains
location-specific knowledge about a Washington State fishing spot.

Extract and describe everything visible:
- Water body name or description if legible
- Named or marked pools, runs, riffles, or holding water
- Access points, parking areas, or trail entry markers
- Wading routes, crossing points, or bank access notes
- Structure markers (logjams, boulders, drop-offs, confluences)
- Compass orientation if indicated; scale or distance markers if present
- All written annotations, labels, or notes on the map (transcribe verbatim in quotes)

Write as dense, searchable prose using standard fly fishing terminology.
Do not speculate about unmarked areas. Do not describe the drawing style.
Focus on information an angler would use to navigate and fish this water.
"""

# ---------------------------------------------------------------------------
# §18.6  Location string extraction  (Llama 3.1 8B — call_json_llm)
# ---------------------------------------------------------------------------

LOCATION_EXTRACTION_PROMPT = """
Extract the fishing location from the note text below.
Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

{{
  "location_string": "",  // the location as mentioned in the note.
                          // Use the most specific name given: river section,
                          // lake name, creek name, or landmark.
                          // Examples: 'lower Yakima below Ellensburg',
                          // 'Lake Valhalla', 'upper Sauk above Clear Creek',
                          // 'the hatchery pool on the Sky'
                          // Return the name as written — do not normalise or abbreviate.
  "confidence": ""        // one of: high | medium | low | none
                          // high: a specific named water body is clearly stated
                          // medium: a location is implied but vague or partial
                          // low: a location is possibly referenced but ambiguous
                          // none: no fishing location can be identified in this text
}}

Rules:
- Return confidence=none and location_string='' if no location is present
- Do not infer a location from species names alone (e.g. 'caught steelhead'
  with no water body named → confidence=none)
- Do not expand abbreviations or correct spelling — return text as written

Note text:
{note_text}
"""

# ---------------------------------------------------------------------------
# Additional prompts added in later phases:
#   §18.1  RECOMMENDATION_SYSTEM_PROMPT       (Phase 5 — context_builder.py)
#   §18.5  DEBRIEF_SUMMARISATION_PROMPT       (Phase 6 — trips/service.py)
#   §18.8  WDFW_REGS_PARSER_PROMPT            (Phase 3 — wdfw_regulations.py)
# ---------------------------------------------------------------------------
