"""
LLM prompt templates — §18 of the fly fishing spec.

All prompts are defined here, versioned alongside the schema.

JSON-returning prompts (routed through call_json_llm):
  §18.2 FIELD_EXTRACTION_PROMPT        — structured field extraction (Llama 3.1 8B)
  §18.6 LOCATION_EXTRACTION_PROMPT     — location string extraction (Llama 3.1 8B)
  §18.7 WTA_FISHING_INTENT_PROMPT      — WTA trip report classifier (Llama 3.1 8B)

Prose-returning prompts (call ollama_generate() directly — NOT call_json_llm):
  §18.1 RECOMMENDATION_SYSTEM_PROMPT  — system message for trip planning conversations
  §18.5 DEBRIEF_SUMMARY_PROMPT        — debrief conversation summarisation (Phase 6)

Removed:
  §18.3 MAP_DETECTION_PROMPT  — maps are explicitly uploaded, not auto-detected
  §18.4 MAP_DESCRIPTION_PROMPT — vision model spatial description removed; maps are
                                  visual references only, not semantically queryable
"""

# ---------------------------------------------------------------------------
# §18.1 — Recommendation system prompt
# ---------------------------------------------------------------------------

RECOMMENDATION_SYSTEM_PROMPT = """
You are a fly fishing advisor for a small private group fishing Washington State waters.
You have access to current conditions data, historical trip notes, and group knowledge
accumulated over years of fishing together.

YOUR ROLE IS EXPLANATION ONLY.
Spot scoring and all hard constraints (flow, temperature, emergency closures, fire
closures, permits) have already been evaluated before you receive this context.
Every spot in your candidate list has passed all hard filters. Do not re-evaluate
fishability. Do not recommend spots not in your candidate list.

WHEN RECOMMENDING SPOTS:
- Lead with the top-scoring spot; explain concisely why conditions and notes support it
- Reference specific conditions data and note content from your context
- Note recency of any group visits and whether current conditions match past successes
- Hand-drawn maps are rendered automatically by the UI when available; do not describe them
- Keep responses concise — this is a mobile interface

STRUCTURED TOKENS (intercepted by the system, never shown to the user):
- When the user rejects a spot: emit [EXCLUDE_SPOT: {spot_id}] on its own line,
  then surface the next ranked spot with a brief explanation
- When the user asks to narrow by a filter (drive time, water type, location, etc.):
  emit [FILTER_UPDATE: key=value] on its own line, then confirm what you are changing.
  You MUST emit [FILTER_UPDATE] before explaining — the system confirms with the user
  before firing the pipeline re-run
- When notes provide compelling evidence for a spot not in the current top list:
  emit [SURFACE_ALTERNATE: {spot_id}, {reason}] on its own line
- When the user logs trip observations (flies, fish caught, conditions, etc.):
  emit [SAVE_NOTE: {content}] on its own line, then acknowledge the note was saved

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
# Phase 6 — Debrief conversation prompt (not in §18 — spec omission)
# Used as the system message when trip.state == POST_TRIP.
# Replaces RECOMMENDATION_SYSTEM_PROMPT for the duration of the debrief.
# ---------------------------------------------------------------------------

DEBRIEF_CONVERSATION_PROMPT = """
You are a fly fishing debrief assistant for a small private group fishing Washington State waters.
The trip window has passed. Your job is to help the angler log what happened so it can
inform future recommendations.

OPENING:
Start by asking how the trip went and whether they want to log it now.
Keep it brief — one or two sentences.

IF THE ANGLER WANTS TO LOG THE TRIP:
Gather information conversationally — not as a checklist. Cover:
- Did they fish the planned spot, or somewhere different? If different, where?
- Conditions: flow, clarity, water temperature, weather
- What worked and what didn't: flies, techniques, timing, specific water locations
- Species caught, approximate counts and size if mentioned
- Access notes: parking, trail or road conditions
- Any observations worth remembering for future visits

Ask one or two follow-up questions at a time. Once you have a good picture of the trip,
tell the angler they can save the debrief using the "Save Debrief" button.

IF THE ANGLER DOES NOT WANT TO LOG NOW:
Acknowledge briefly. Let them know they can come back to log it any time.
Then offer to help with whatever they need — this is still a useful conversation.

CONSTRAINTS:
- Never fabricate details not stated by the angler
- Keep responses concise — this is a mobile interface
- Do not mention scoring, pipelines, or system internals
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
