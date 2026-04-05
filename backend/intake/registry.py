"""
Intake question registry — single source of truth for all intake questions.

PROFILE_QUESTIONS: collected at onboarding, editable in Settings (§8.1).
SESSION_QUESTIONS: structured intake card at each new trip (§8.2).

Each question has:
  id        — key used in preferences/session_intake JSONB
  question  — display text
  type      — "single_select" | "multi_select" | "location"
  options   — list of option dicts {value, label} (omitted for location type)
"""

PROFILE_QUESTIONS = [
    {
        "id": "home_location",
        "question": "Where do you typically depart from?",
        "type": "location",
        # Stored as {lat, lon, label} — used for HERE drive-time calculations
    },
    {
        "id": "vehicle_capability",
        "question": "What is your vehicle capability?",
        "type": "single_select",
        "options": [
            {"value": "paved_only", "label": "Paved / 2WD only"},
            {"value": "dirt_ok", "label": "Dirt road OK"},
            {"value": "four_wd", "label": "4WD / High clearance"},
        ],
    },
    {
        "id": "experience_level",
        "question": "How would you describe your fly fishing experience?",
        "type": "single_select",
        "options": [
            {"value": "beginner", "label": "Beginner"},
            {"value": "intermediate", "label": "Intermediate"},
            {"value": "advanced", "label": "Advanced"},
        ],
    },
    {
        "id": "catch_intent",
        "question": "What is your catch intent?",
        "type": "single_select",
        "options": [
            {"value": "catch_and_release", "label": "Strict catch-and-release"},
            {"value": "keep_if_legal", "label": "Keep if legal"},
        ],
    },
    {
        "id": "gear_setup",
        "question": "What gear setups do you typically fish with?",
        "type": "multi_select",
        "options": [
            {"value": "full_setup", "label": "Full setup"},
            {"value": "pack_rod", "label": "Pack rod"},
            {"value": "float_tube", "label": "Float tube"},
            {"value": "spey", "label": "Spey / two-hander"},
        ],
    },
]

SESSION_QUESTIONS = [
    {
        "id": "departure_time",
        "question": "When are you leaving and returning?",
        "type": "datetime_range",
    },
    {
        "id": "water_type",
        "question": "What type of water are you looking for?",
        "type": "multi_select",
        "options": [
            {"value": "river", "label": "River"},
            {"value": "creek", "label": "Creek"},
            {"value": "lake", "label": "Lake"},
            {"value": "saltwater", "label": "Saltwater"},
        ],
    },
    {
        "id": "target_species",
        "question": "What species are you targeting?",
        "type": "multi_select",
        "options": [
            {"value": "steelhead", "label": "Steelhead"},
            {"value": "trout", "label": "Trout"},
            {"value": "salmon", "label": "Salmon"},
            {"value": "cutthroat", "label": "Cutthroat"},
            {"value": "bass", "label": "Bass"},
            {"value": "any", "label": "Any"},
        ],
    },
    {
        "id": "trip_goal",
        "question": "What is your main goal for this trip?",
        "type": "single_select",
        "options": [
            {"value": "maximise_catch", "label": "Maximise catch"},
            {"value": "explore", "label": "Explore new water"},
            {"value": "relax", "label": "Relax / scenic"},
            {"value": "teach", "label": "Teach beginners"},
        ],
    },
    {
        "id": "trail_difficulty",
        "question": "What trail difficulty can you handle?",
        "type": "single_select",
        "options": [
            {"value": "easy", "label": "Easy"},
            {"value": "moderate", "label": "Moderate"},
            {"value": "strenuous", "label": "Strenuous"},
            {"value": "any", "label": "Any"},
        ],
    },
]
