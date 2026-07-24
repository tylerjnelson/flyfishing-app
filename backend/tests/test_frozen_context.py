"""
Phase 4 validation — frozen prefix + lean follow-ups.

Two layers:
  1. The pure formatters (`_format_spot_bundle`, `_format_compact_menu`, notes/
     regs helpers) — the byte content of the freeze, tested directly.
  2. `_build_agentic_context` — the freeze-once / replay-verbatim / TTL-refresh /
     as-of-stamp control flow, with the DB-touching freeze + conditions-refresh
     mocked so the logic is exercised without Postgres (the freeze reads are
     covered by the formatter tests; here we test *when* it freezes vs replays).

The load-bearing property (plan Phase 4 validation): the assembled system-message
prefix is byte-identical across two consecutive follow-up turns.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone

from chat import context_builder as cb


# ---------------------------------------------------------------------------
# Pure formatters
# ---------------------------------------------------------------------------

def _candidate(cfs=450, trend="rising", name="Yakima", sid=None, drive=45):
    return {
        "fishing_spot_id": sid or str(uuid.uuid4()),
        "water_body_id": str(uuid.uuid4()),
        "spot_name": name,
        "water_body_name": name,
        "spot_type": "river",
        "drive_minutes": drive,
        "is_haversine": False,
        "straight_line_miles": None,
        "warnings": [],
        "conditions": {"usgs": {"cfs": cfs, "trend": trend, "temp_f": 52.0}},
    }


class TestSpotBundle:
    def test_bundle_has_conditions_details_and_notes(self):
        c = _candidate()
        notes = [{
            "note_date": "2025-09-12", "outcome": "positive",
            "species": ["brown trout"], "approx_cfs": 460,
            "flies": ["hopper-dropper"], "content": "worked mid-morning near the seam",
        }]
        details = {
            "fishing_regs": {"summary": "selective gear, catch-and-release"},
            "fly_fishing_legal": True,
            "species_primary": ["rainbow trout", "cutthroat"],
            "last_stocked_date": "2025-05-01",
            "last_stocked_species": ["rainbow trout"],
            "permit_required": True,
            "permit_notes": "WA discover pass",
        }
        out = cb._format_spot_bundle(c, notes=notes, details=details)

        assert "=== Yakima ===" in out
        assert "Flow: 450 CFS (rising)" in out
        assert "Fly-only: yes" in out
        assert "Species: rainbow trout, cutthroat" in out
        assert "Last stocked: 2025-05-01 (rainbow trout)" in out
        assert "Permit required — WA discover pass" in out
        # notes structured-fields-first, then snippet
        assert "Notes (≤1 recent):" in out
        assert "POSITIVE" in out and "brown trout" in out
        assert "flies: hopper-dropper" in out
        assert "worked mid-morning" in out

    def test_bundle_without_notes_or_details_is_just_conditions(self):
        out = cb._format_spot_bundle(_candidate(), notes=[], details={})
        assert "Flow: 450 CFS" in out
        assert "Notes" not in out

    def test_note_line_orders_structured_fields_first(self):
        line = cb._format_note_line({
            "note_date": "2025-08-01", "outcome": "negative",
            "species": ["steelhead"], "approx_cfs": 300, "flies": ["egg"],
            "content": "blown out",
        })
        assert line.startswith("[2025-08-01] NEGATIVE · steelhead · ~300 cfs · flies: egg — ")
        assert line.endswith('"blown out"')

    def test_regs_str_handles_dict_and_string(self):
        assert cb._regs_str({"summary": "C&R only"}) == "C&R only"
        assert cb._regs_str("no bait") == "no bait"
        assert cb._regs_str(None) == ""


class TestCompactMenu:
    def test_menu_lists_only_alternates_past_top_3(self):
        cands = [_candidate(name=f"S{i}", cfs=100 + i) for i in range(6)]
        menu = cb._format_compact_menu(cands)
        # top-3 (S0,S1,S2) are full bundles, not in the menu; S3,S4,S5 are.
        assert "S0" not in menu and "S1" not in menu and "S2" not in menu
        for i in (3, 4, 5):
            assert f"S{i}" in menu
        # one line per alternate + header
        assert menu.count("\n- ") == 3
        assert "MORE OPTIONS" in menu

    def test_menu_empty_when_no_alternates(self):
        assert cb._format_compact_menu([_candidate() for _ in range(3)]) == ""

    def test_menu_line_has_id_name_type_drive_teaser(self):
        cands = [_candidate() for _ in range(3)] + [
            _candidate(name="Cle Elum", cfs=800, trend="falling", drive=90)
        ]
        menu = cb._format_compact_menu(cands)
        assert "Cle Elum (river)" in menu
        assert "90 min" in menu
        assert "800 cfs (falling)" in menu


# ---------------------------------------------------------------------------
# _build_agentic_context — freeze / replay / TTL / stamp
# ---------------------------------------------------------------------------

class _FakeResult:
    def all(self):
        return []          # empty transcript


class _FakeDB:
    async def execute(self, *a, **k):
        return _FakeResult()


def _ctx_objs(frozen=None, frozen_at=None):
    conv = types.SimpleNamespace(
        id=uuid.uuid4(), frozen_context=frozen, frozen_context_at=frozen_at
    )
    user = types.SimpleNamespace(id=uuid.uuid4())
    trip = types.SimpleNamespace(id=uuid.uuid4())
    return conv, user, trip


async def _build(monkeypatch, conv, user, trip, *, candidates=None, force_rerun=False):
    freeze_calls = {"n": 0}
    refresh_calls = {"n": 0}

    async def _fake_freeze(db, u, cands, dep):
        freeze_calls["n"] += 1
        return f"FROZEN::{freeze_calls['n']}"

    async def _fake_refresh(db, cands):
        refresh_calls["n"] += 1
        return cands

    monkeypatch.setattr(cb, "_freeze_context", _fake_freeze)
    monkeypatch.setattr(cb, "_refresh_candidate_conditions", _fake_refresh)

    result = await cb._build_agentic_context(
        user, trip, conv, "how's the flow?", _FakeDB(),
        candidates=candidates or [_candidate()],
        drive_time_unavailable=False,
        intake_hash="ih",
        departure_time=datetime.now(tz=timezone.utc),
        force_rerun=force_rerun,
    )
    return result, freeze_calls, refresh_calls


class TestAgenticAssembly:
    async def test_opening_turn_freezes_and_stores(self, monkeypatch):
        conv, user, trip = _ctx_objs(frozen=None)
        result, freeze_calls, _ = await _build(monkeypatch, conv, user, trip)

        assert freeze_calls["n"] == 1               # froze once
        assert conv.frozen_context == "FROZEN::1"    # stored on the conversation
        assert conv.frozen_context_at is not None
        sys_msg = result.messages[0]["content"]
        assert "FROZEN::1" in sys_msg
        assert "[Conditions as of" in sys_msg        # as-of stamp
        # agentic never writes the single-shot response cache
        assert result.conditions_hash is None
        assert result.cached_response is None

    async def test_follow_up_replays_verbatim_no_refreeze(self, monkeypatch):
        # Fresh frozen prefix already present -> two consecutive follow-ups must
        # reuse it byte-for-byte (the Phase 4 load-bearing property).
        at = datetime.now(tz=timezone.utc) - timedelta(minutes=5)
        conv, user, trip = _ctx_objs(frozen="STORED_PREFIX", frozen_at=at)

        r1, freeze_calls, _ = await _build(monkeypatch, conv, user, trip)
        r2, _, _ = await _build(monkeypatch, conv, user, trip)

        assert freeze_calls["n"] == 0                       # never re-froze
        assert "STORED_PREFIX" in r1.messages[0]["content"]
        assert r1.messages[0]["content"] == r2.messages[0]["content"]  # byte-identical

    async def test_ttl_expiry_triggers_here_free_refreeze(self, monkeypatch):
        stale_at = datetime.now(tz=timezone.utc) - timedelta(
            hours=cb._FREEZE_TTL_HOURS + 1
        )
        conv, user, trip = _ctx_objs(frozen="OLD", frozen_at=stale_at)
        result, freeze_calls, refresh_calls = await _build(monkeypatch, conv, user, trip)

        assert freeze_calls["n"] == 1                # re-froze past the TTL
        assert refresh_calls["n"] == 1              # conditions refreshed (HERE-free)
        assert conv.frozen_context == "FROZEN::1"
        assert "OLD" not in result.messages[0]["content"]

    async def test_within_ttl_does_not_refresh(self, monkeypatch):
        at = datetime.now(tz=timezone.utc) - timedelta(hours=cb._FREEZE_TTL_HOURS - 1)
        conv, user, trip = _ctx_objs(frozen="STILL_FRESH", frozen_at=at)
        _, freeze_calls, refresh_calls = await _build(monkeypatch, conv, user, trip)

        assert freeze_calls["n"] == 0
        assert refresh_calls["n"] == 0

    async def test_force_rerun_refreezes_without_conditions_refresh(self, monkeypatch):
        # confirm-filter re-run: re-freeze from the freshly rebuilt candidates, but
        # do NOT take the TTL conditions-refresh path.
        at = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        conv, user, trip = _ctx_objs(frozen="OLD", frozen_at=at)
        result, freeze_calls, refresh_calls = await _build(
            monkeypatch, conv, user, trip, force_rerun=True
        )
        assert freeze_calls["n"] == 1
        assert refresh_calls["n"] == 0
        assert conv.frozen_context == "FROZEN::1"

    async def test_transcript_replayed_between_system_and_query(self, monkeypatch):
        conv, user, trip = _ctx_objs(frozen="P", frozen_at=datetime.now(tz=timezone.utc))
        result, _, _ = await _build(monkeypatch, conv, user, trip)
        # [system, ...transcript (empty here)..., user]
        assert result.messages[0]["role"] == "system"
        assert result.messages[-1] == {"role": "user", "content": "how's the flow?"}
