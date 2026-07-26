"""
Phase 7 validation — contracts & UX *through the agentic loop* (nothing regresses).

The structured-token contracts ([RECOMMEND]/[FILTER_UPDATE]/[SAVE_NOTE]) and the
spot-card / exclude / confirm plumbing are unit-tested in isolation elsewhere
(test_streaming, test_turn_builder). Phase 7's concern is that they still hold when
driven through the Phase-3 agentic loop and its shared StreamHandler:

  * StreamHandler accumulates only the loop's forwarded ``content`` — reasoning is
    on a channel the engine adapter never yields, and tool hops carry no prose, so
    structured tokens are parsed off the final answer hop only.
  * turn_builder.build_turn renders cards for recommended spots drawn from the
    *paged* menu (spots 4-25), not just the frozen top-3 — they're already in
    session_candidates, so cards fill in by id.
  * /chat/exclude-spot and /chat/confirm-filter still operate on the conversation's
    session_candidates / excluded_spot_ids / pending_filter_update (and exclude-spot
    busts the Phase-4 frozen prefix). The confirm-filter Yes-path re-runs the
    pipeline (HERE) and is left to the Phase 8 integration gate with stubs; only the
    HERE-free reject path is exercised here.

Engine + tool exec + persistence are mocked at their seams (mirroring Phase 3/5/6),
so these run with no live server, DB, or HERE call.
"""

import json
import types
import uuid

import pytest

from chat import router
from chat.streaming import StreamHandler
from chat.turn_builder import build_turn, parse_recommend_block


# ---------------------------------------------------------------------------
# Agentic-loop harness (mirrors test_agentic_loop)
# ---------------------------------------------------------------------------

def _sentinel(tool_calls=None, tokens=0, finish="stop"):
    return {"_done": True, "token_count": tokens, "tool_calls": tool_calls or [], "finish_reason": finish}


def _tool_call(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args or {}}}


def _fake_stream_hop(scripts):
    state = {"i": 0, "tools_seen": []}

    async def _hop(messages, *, tools):
        script = scripts[state["i"]]
        state["i"] += 1
        state["tools_seen"].append(tools)
        for item in script:
            yield item

    return _hop, state


async def _run_loop(monkeypatch, scripts):
    """Drive `_run_agentic_loop` with a scripted stream + mocked tool exec/persist."""
    hop, hop_state = _fake_stream_hop(scripts)

    async def _fake_execute_tool(name, arguments, **kwargs):
        return {"tool": name, "ok": True}, f"{name}-digest"

    async def _fake_persist(db, conversation_id, tool_calls, results):
        pass

    monkeypatch.setattr(router, "_stream_hop", hop)
    monkeypatch.setattr(router, "execute_tool", _fake_execute_tool)
    monkeypatch.setattr(router, "_persist_tool_turn", _fake_persist)

    handler = StreamHandler([])
    conv = types.SimpleNamespace(id=uuid.uuid4(), excluded_spot_ids=None)
    trip = types.SimpleNamespace(id=uuid.uuid4())

    events = []
    async for evt in router._run_agentic_loop(
        messages=[{"role": "user", "content": "hi"}],
        handler=handler, conversation=conv, trip=trip, db=None,
    ):
        events.append(json.loads(evt[len("data: "):].strip()))
    return events, handler, hop_state


def _tokens(events):
    return [e["content"] for e in events if e["type"] == "token"]


# ---------------------------------------------------------------------------
# Contract 1 — StreamHandler parses structured tokens on the final answer hop
# ---------------------------------------------------------------------------

class TestStructuredTokensThroughLoop:
    async def test_recommend_block_captured_after_a_tool_hop(self, monkeypatch):
        # A tool hop (no prose) then the answer hop emitting prose + a fragmented
        # [RECOMMEND] block. Only the prose is forwarded; the block is intercepted.
        u1, u2, u3 = (str(uuid.uuid4()) for _ in range(3))
        scripts = [
            [_sentinel(tool_calls=[_tool_call("get_spot", {"spot_id": "x"})])],
            [
                "Try the Yakima and two more. ",
                "[RECOMMEND: ", f"{u1}, {u2}, ", f"{u3}]",
                _sentinel(),
            ],
        ]
        events, handler, hop_state = await _run_loop(monkeypatch, scripts)

        assert _tokens(events) == ["Try the Yakima and two more. "]
        assert "[RECOMMEND" not in handler.full_response
        assert handler.recommend_block is not None
        _, uuids = parse_recommend_block(handler.recommend_block)
        assert uuids == [u1, u2, u3]
        assert hop_state["i"] == 2

    async def test_only_answer_hop_content_reaches_handler(self, monkeypatch):
        # The "final content hop only" guarantee: a preceding tool hop contributes
        # no content, so full_response holds exactly the answer-hop prose.
        scripts = [
            [_sentinel(tool_calls=[_tool_call("compare_spots")])],
            ["The Yakima is fishing best.", _sentinel()],
        ]
        _, handler, _ = await _run_loop(monkeypatch, scripts)
        assert handler.full_response == "The Yakima is fishing best."

    async def test_filter_update_through_loop_sets_pending_and_final_event(self, monkeypatch):
        # [FILTER_UPDATE] on the answer hop is stripped from the stream, recorded, and
        # surfaced by on_stream_end as filter_confirmation_required (as the router does).
        scripts = [[
            "Sounds good. ", "[FILTER_UPDATE: max_drive_minutes=90]", " Done.",
            _sentinel(),
        ]]
        events, handler, _ = await _run_loop(monkeypatch, scripts)

        assert "".join(_tokens(events)) == "Sounds good.  Done."
        assert handler.pending_filter_update == {"key": "max_drive_minutes", "value": "90"}
        assert handler.on_stream_end() == {
            "event": "filter_confirmation_required",
            "key": "max_drive_minutes",
            "value": "90",
        }

    async def test_save_note_through_loop_captured(self, monkeypatch):
        scripts = [[
            "Nice one. ", "[SAVE_NOTE: 3 cutthroat on a size 16 caddis]", " Logged.",
            _sentinel(),
        ]]
        _, handler, _ = await _run_loop(monkeypatch, scripts)
        assert handler.save_note_contents == ["3 cutthroat on a size 16 caddis"]
        assert "[SAVE_NOTE" not in handler.full_response


# ---------------------------------------------------------------------------
# Contract 2 — turn_builder renders cards for paged-menu spots (4-25)
# ---------------------------------------------------------------------------

def _candidate(i):
    return {
        "spot_id": str(uuid.uuid4()),
        "spot_name": f"Spot {i}",
        "water_body_name": f"River {i}",
        "spot_type": "river",
        "drive_minutes": 30 + i,
        "is_haversine": False,
        "straight_line_miles": None,
        "session_score": 9.0 - i * 0.1,
        "last_visited": None,
        "warnings": [],
        "conditions": {},
    }


class _EmptyResult:
    def __iter__(self):
        return iter(())


class _EmptyDB:
    """db.execute() → no FishingSpot/Note rows; cards must fill from candidates."""
    async def execute(self, *a, **k):
        return _EmptyResult()


class TestTurnBuilderPagedSpots:
    async def test_renders_cards_for_spots_beyond_top_3(self):
        # A 12-spot menu; recommend three drawn from the paged tail (indices 5/8/11).
        candidates = [_candidate(i) for i in range(12)]
        picks = [candidates[5], candidates[8], candidates[11]]
        block = "[RECOMMEND: {}, {}, {}]".format(*[c["spot_id"] for c in picks])

        turn = await build_turn(
            narrative="Here are three off the menu.",
            recommend_block=block,
            candidates=candidates,
            db=_EmptyDB(),
        )

        assert turn.get("error") is None
        assert [c["spot_id"] for c in turn["cards"]] == [c["spot_id"] for c in picks]
        # Card data is pulled from the candidate by id regardless of its menu rank.
        assert [c["name"] for c in turn["cards"]] == ["Spot 5", "Spot 8", "Spot 11"]
        assert turn["cards"][0]["drive_minutes"] == 35


# ---------------------------------------------------------------------------
# Contract 3 — exclude-spot / confirm-filter operate on conversation state
# ---------------------------------------------------------------------------

class _ConvResult:
    def __init__(self, conv):
        self._conv = conv

    def scalar_one_or_none(self):
        return self._conv


class _ConvDB:
    def __init__(self, conv):
        self._conv = conv
        self.commits = 0

    async def execute(self, *a, **k):
        return _ConvResult(self._conv)

    async def commit(self):
        self.commits += 1


class TestExcludeAndConfirmContracts:
    async def test_exclude_spot_removes_appends_and_busts_frozen(self):
        a, b, c = (str(uuid.uuid4()) for _ in range(3))
        conv = types.SimpleNamespace(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            session_candidates={"candidates": [{"spot_id": a}, {"spot_id": b}, {"spot_id": c}]},
            excluded_spot_ids=[],
            frozen_context="FROZEN::1",
            frozen_context_at="2026-07-24T00:00:00Z",
        )
        user = types.SimpleNamespace(id=conv.user_id)
        db = _ConvDB(conv)

        out = await router.exclude_spot_endpoint(
            {"conversation_id": str(conv.id), "spot_id": b}, user=user, db=db,
        )

        # Removed from candidates, appended to excluded, frozen prefix busted (Phase 4).
        assert [x["spot_id"] for x in out["candidates"]] == [a, c]
        assert conv.session_candidates["candidates"] == [{"spot_id": a}, {"spot_id": c}]
        assert uuid.UUID(b) in conv.excluded_spot_ids
        assert conv.frozen_context is None
        assert conv.frozen_context_at is None

    async def test_confirm_filter_reject_clears_pending_leaves_candidates(self, monkeypatch):
        # The HERE-free No-path: clears the pending update, leaves candidates untouched.
        candidates = {"candidates": [{"spot_id": str(uuid.uuid4())}]}
        conv = types.SimpleNamespace(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            trip_id=uuid.uuid4(),
            pending_filter_update={"key": "max_drive_minutes", "value": "90"},
            session_candidates=candidates,
        )
        user = types.SimpleNamespace(id=conv.user_id)
        db = _ConvDB(conv)

        async def _fake_get_trip(trip_id, user_id, db):
            return types.SimpleNamespace(id=conv.trip_id, session_intake={})

        monkeypatch.setattr(router, "get_trip", _fake_get_trip)

        out = await router.confirm_filter_endpoint(
            {"conversation_id": str(conv.id), "confirm": False}, user=user, db=db,
        )

        assert out["result"] == "rejected"
        assert out["session_candidates"] == candidates      # unchanged
        assert conv.pending_filter_update is None            # cleared


# ---------------------------------------------------------------------------
# Contract 4 — commit-spot ("lock this spot") sets trip.fishing_spot_id
# ---------------------------------------------------------------------------

class TestCommitSpotContract:
    """The lock-this-spot flow: resolve the card's fishing_spot_id from the
    candidate and set trip.fishing_spot_id via assign_spot. Purely additive —
    no candidate mutation, no state transition; re-locking overwrites."""

    def _conv(self, candidates):
        return types.SimpleNamespace(
            id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            trip_id=uuid.uuid4(),
            session_candidates={"candidates": candidates},
        )

    def _wire(self, monkeypatch, trip):
        async def _fake_get_trip(trip_id, user_id, db):
            return trip

        async def _fake_assign_spot(t, fishing_spot_id, db):
            t.fishing_spot_id = fishing_spot_id
            return t

        monkeypatch.setattr(router, "get_trip", _fake_get_trip)
        monkeypatch.setattr(router, "assign_spot", _fake_assign_spot)

    async def test_commit_resolves_fishing_spot_id_and_sets_trip(self, monkeypatch):
        spot_a, fsid_a = str(uuid.uuid4()), str(uuid.uuid4())
        conv = self._conv([{"spot_id": spot_a, "fishing_spot_id": fsid_a}])
        user = types.SimpleNamespace(id=conv.user_id)
        trip = types.SimpleNamespace(id=conv.trip_id, fishing_spot_id=None)
        self._wire(monkeypatch, trip)

        out = await router.commit_spot_endpoint(
            {"conversation_id": str(conv.id), "spot_id": spot_a}, user=user, db=_ConvDB(conv),
        )

        # Returns the real FishingSpot UUID (NOT the card key spot_id) and sets the column.
        assert out["fishing_spot_id"] == fsid_a
        assert str(trip.fishing_spot_id) == fsid_a
        # Candidates untouched — commit is purely additive.
        assert conv.session_candidates["candidates"] == [{"spot_id": spot_a, "fishing_spot_id": fsid_a}]

    async def test_commit_unknown_spot_id_404(self, monkeypatch):
        conv = self._conv([{"spot_id": str(uuid.uuid4()), "fishing_spot_id": str(uuid.uuid4())}])
        user = types.SimpleNamespace(id=conv.user_id)
        self._wire(monkeypatch, types.SimpleNamespace(id=conv.trip_id, fishing_spot_id=None))

        with pytest.raises(router.HTTPException) as ei:
            await router.commit_spot_endpoint(
                {"conversation_id": str(conv.id), "spot_id": str(uuid.uuid4())},
                user=user, db=_ConvDB(conv),
            )
        assert ei.value.status_code == 404

    async def test_commit_overwrites_existing_lock(self, monkeypatch):
        spot_a, fsid_a = str(uuid.uuid4()), str(uuid.uuid4())
        spot_b, fsid_b = str(uuid.uuid4()), str(uuid.uuid4())
        conv = self._conv([
            {"spot_id": spot_a, "fishing_spot_id": fsid_a},
            {"spot_id": spot_b, "fishing_spot_id": fsid_b},
        ])
        user = types.SimpleNamespace(id=conv.user_id)
        trip = types.SimpleNamespace(id=conv.trip_id, fishing_spot_id=uuid.UUID(fsid_a))
        self._wire(monkeypatch, trip)

        out = await router.commit_spot_endpoint(
            {"conversation_id": str(conv.id), "spot_id": spot_b}, user=user, db=_ConvDB(conv),
        )
        assert out["fishing_spot_id"] == fsid_b
        assert str(trip.fishing_spot_id) == fsid_b

    async def test_commit_missing_body_400(self, monkeypatch):
        conv = self._conv([{"spot_id": str(uuid.uuid4()), "fishing_spot_id": str(uuid.uuid4())}])
        user = types.SimpleNamespace(id=conv.user_id)
        with pytest.raises(router.HTTPException) as ei:
            await router.commit_spot_endpoint(
                {"conversation_id": str(conv.id)}, user=user, db=_ConvDB(conv),
            )
        assert ei.value.status_code == 400
