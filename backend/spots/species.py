"""
One species vocabulary (DATA-INTAKE DI-0).

The intake asks for groups (intake/registry.py target_species: trout, steelhead,
salmon, cutthroat, bass, any); water bodies and WDFW stocking records name fish
('Rainbow', 'Brown Trout', 'Eastern Brook Trout', 'Coho', 'Kokanee'). groups()
maps a fish name to the intake groups it satisfies, so a trout trip matches a
lake stocked with rainbows.
"""

import re

ANY = "any"
INTAKE_GROUPS = frozenset({"trout", "steelhead", "salmon", "cutthroat", "bass"})

# Patterns are tried against the lowercased name; every hit adds its groups.
_GROUPS: list[tuple[re.Pattern, frozenset[str]]] = [
    (re.compile(r"\bsteelhead\b"), frozenset({"steelhead"})),
    (re.compile(r"\bcutthroat\b|\bcutt\b"), frozenset({"trout", "cutthroat"})),
    (re.compile(r"\b(rainbow|brown|brook|tiger trout|golden|bull trout|dolly varden|lake trout|mackinaw|"
                r"redband|trout|char|grayling)\b"), frozenset({"trout"})),
    (re.compile(r"\b(coho|chinook|king|chum|pink|sockeye|salmon)\b"), frozenset({"salmon"})),
    (re.compile(r"\b(smallmouth|largemouth|bass)\b"), frozenset({"bass"})),
]


def groups(name: str | None) -> frozenset[str]:
    """Intake groups a fish name belongs to; empty when it fits none (kokanee,
    catfish, tiger muskie, whitefish)."""
    s = (name or "").strip().lower()
    out: set[str] = set()
    for pat, gs in _GROUPS:
        if pat.search(s):
            out |= gs
    if "steelhead" in out:
        out.discard("trout")  # 'steelhead trout' is a steelhead trip, not a trout one
    return frozenset(out)


def targets(target_species: list[str] | None) -> set[str]:
    """The intake answer as groups; empty when the angler takes anything."""
    t = {(s or "").strip().lower() for s in target_species or []}
    if not t or ANY in t:
        return set()
    out: set[str] = set()
    for s in t:
        # An intake value is its own group ('cutthroat' wants cutthroat, not any trout);
        # a fish name ('rainbow') becomes its groups.
        out |= {s} if s in INTAKE_GROUPS else (groups(s) or {s})
    return out


def water_groups(*species_lists) -> set[str]:
    """Groups present at a water from any of its species lists."""
    return {g for lst in species_lists for s in lst or [] for g in groups(s)}
