"""
Intake service — profile + session context merge.

Session values override profile values where both exist (§8.2).
Result is stored as trips.session_intake JSONB.
"""


def merge_intake(profile_preferences: dict, session_intake: dict) -> dict:
    """
    Merge profile-scoped preferences with session-scoped answers.
    Session values take precedence over profile defaults.
    """
    merged = {**(profile_preferences or {}), **(session_intake or {})}
    return merged
