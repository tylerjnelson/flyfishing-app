"""
LLM prompt templates — §18 of the fly fishing spec.

All prompts are defined here, versioned alongside the schema.

JSON-returning prompts (routed through call_json_llm):
  §18.2 FIELD_EXTRACTION_PROMPT        — structured field extraction (Llama 3.1 8B)
  §18.6 LOCATION_EXTRACTION_PROMPT     — location string extraction (Llama 3.1 8B)
  §18.7 WTA_FISHING_INTENT_PROMPT      — WTA trip report classifier (Llama 3.1 8B)

Prose-returning prompts (call ollama_generate() directly — NOT call_json_llm):
  §18.1 RECOMMENDATION_SYSTEM_PROMPT  — system message for trip planning conversations
  §18.4 MAP_DESCRIPTION_PROMPT        — spatial description of uploaded map image
  §18.5 DEBRIEF_SUMMARY_PROMPT        — debrief conversation summarisation

Note: MAP_DETECTION_PROMPT (§18.3) was removed. Maps are now explicitly uploaded
by the user rather than auto-detected from handwritten note photos.
"""

# ---------------------------------------------------------------------------
# §18.1 — Recommendation system prompt
# ---------------------------------------------------------------------------

RECOMMENDATION_SYSTEM_PROMPT = """
You are a fly fishing advisor for a small private group fishing Washington State waters.
You have access to current conditions data, historical trip notes, and group knowledge
accumulated over years of fishing together.

YOUR ROLE IS EXPLANATION ONLY.
Generate natural language around these tokens. They are never visible to the user.

CONSTRAINTS:
- Never fabricate conditions data, flow readings, or note content
- Never describe a closed spot as fishable
- If information is missing from your context, say so — do not infer
- Never mention the scoring system, weights, or pipeline internals
"""

# ---------------------------------------------------------------------------
# §18.2 — Structured field extraction prompt
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
  "approx_cfs": null,   // numeric flow in CFS if mentioned, else null
  "approx_temp": null,  // numeric water temp in °F if mentioned, else null
  "time_of_day": null   // one of: morning | afternoon | evening | all-day | null
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
# §18.4 — Map spatial description prompt
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
# §18.5 — Debrief summarisation prompt
# ---------------------------------------------------------------------------

DEBRIEF_SUMMARY_PROMPT = """
Summarise the following fishing trip debrief conversation as a cohesive prose note,
written in first-person from the angler's perspective.

This note will be stored in a group fishing knowledge base and used to inform
future trip recommendations.

Write 2–4 paragraphs covering:
- Conditions encountered: flow, clarity, temperature, weather
- What worked and what did not: flies, techniques, timing, locations on the water
- Fish species, counts, and size if mentioned
- Access notes, parking, trail conditions if discussed
- Any observations worth remembering for future visits

Do not include meta-commentary about the conversation.
Do not use headers or bullet points. Write as field notes.
Do not fabricate detail not present in the conversation.

Conversation:
{conversation_text}
"""

# ---------------------------------------------------------------------------
# §18.6 — Location string extraction prompt
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
# §18.7 — WTA fishing-intent classifier prompt
# ---------------------------------------------------------------------------

WTA_FISHING_INTENT_PROMPT = """
Determine whether the following WTA trip report describes active fly fishing
or fishing as a primary or secondary activity.

Return ONLY valid JSON. No preamble. No markdown fences. No explanation.

{
  "fishing_intent": true | false,
  "confidence": "high" | "medium" | "low",
  "evidence": ""  // one sentence quoting or summarising the fishing signal,
                  // or 'none' if fishing_intent is false
}

fishing_intent = true when the report:
- Explicitly mentions fishing, fly fishing, casting, catching fish, or fishing gear
- Describes fish species encountered in a fishing context (not wildlife observation)
- Recommends the location for fishing

fishing_intent = false when the report:
- Only mentions water features incidentally (e.g. 'crossed a stream')
- Mentions fish as wildlife without a fishing context

Trip report:
{report_text}
"""
