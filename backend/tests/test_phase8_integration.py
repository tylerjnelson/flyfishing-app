"""
Phase 8 integration gate — the loop *at the endpoint seam*, end to end.

Phases 3-7 validated their pieces at unit boundaries (loop, digests, budget guard,
contract parsing, card building). Phase 8 drives the actual `chat_endpoint` /
`confirm_filter_endpoint` coroutines — draining the real `StreamingResponse` — to
prove the seams the earlier phases deferred here still hold when a whole turn runs:

  * a conversation closed on a prior turn (and one closed *this* turn by the budget
    guard) refuses at the endpoint before the engine is ever invoked or a user row
    is persisted;
  * a full agentic turn — engine hop → real `execute_tool` → answer — touches **no
    HERE** surface (the standing §11.1 constraint, proven through the loop, not just
    asserted of the catalog);
  * exclusion is honored both inside the loop (`get_spot` short-circuits a set-aside
    spot with no DB read) AND on the `confirm-filter` Yes-path pipeline re-run, which
    now hands `build_context` the conversation's `excluded_spot_ids` so the hard
    filter drops them (the former orthogonal gap, now fixed — the pure filter itself
    is unit-tested in test_context_builder).

The engine (`_stream_hop`), persistence (`_persist_tool_turn`, `_next_seq`), the
session-start pipeline (`build_context`), and trip lookup are mocked at their seams —
mirroring Phases 3/5/6/7 — so this runs with no live server, DB, or HERE call. The
live half of Phase 8 (multi-turn against a real llama.cpp server, two-pass-vs-loop
latency/KV/contract measurement, and the deploy + flag-flip) needs the Phase 1 engine
deployed and a maintenance window and is tracked as deploy-blocked in the plan doc.
"""

import json
import types
import uuid

import pytest

from chat import router
from chat.tools import TOOL_SCHEMAS, execute_tool
from chat.context_builder import CONTEXT_HARD_TOKENS


# ---------------------------------------------------------------------------
# Endpoint-driving harness
# ---------------------------------------------------------------------------

class _Result:
    """A db.execute() result: serves the conversation for scalar_one_or_none and is
    also empty-iterable (so a stray SELECT never explodes)."""

    def __init__(self, conv):
        self._conv = conv

    def scalar_one_or_none(self):
        return self._conv

    def __iter__(self):
        return iter(())


class _FakeDB:
    def __init__(self, conv):
        self._conv = conv
        self.added = []
        self.commits = 0

    async def execute(self, *a, **k):
        return _Result(self._conv)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _candidate(fishing_spot_id, spot_id=None, name="Yakima River"):
    return {
        "spot_id": spot_id or str(uuid.uuid4()),
        "fishing_spot_id": fishing_spot_id,
        "spot_name": name,
        "water_body_name": name,
        "spot_type": "river",
        "drive_minutes": 40,
        "is_haversine": False,
        "straight_line_miles": None,
        "session_score": 8.0,
        "last_visited": None,
        "warnings": [],
        "conditions": {},
    }


def _build_result(messages, candidates):
    return types.SimpleNamespace(
        messages=messages,
        session_candidates={"candidates": candidates},
        conditions_hash=None,
        intake_hash=None,
        drive_time_unavailable=False,
        cached_response=None,
    )


def _sentinel(tool_calls=None, tokens=3):
    return {"_done": True, "token_count": tokens, "tool_calls": tool_calls or [], "finish_reason": "stop"}


def _tool_call(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name, "arguments": args or {}}}


def _scripted_hop(scripts):
    """A fake `_stream_hop`: each call replays the next script (a list of token
    strings terminated by a `_done` sentinel). Records the `tools=` kwarg per hop."""
    state = {"i": 0, "tools_seen": []}

    async def _hop(messages, *, tools):
        script = scripts[state["i"]]
        state["i"] += 1
        state["tools_seen"].append(tools)
        for item in script:
            yield item

    return _hop, state


async def _drain(resp):
    """Consume a StreamingResponse's body and parse each SSE frame to a dict."""
    events = []
    async for chunk in resp.body_iterator:
        events.append(json.loads(chunk[len("data: "):].strip()))
    return events


def _install_common(monkeypatch, *, trip, build_result=None, engine=None):
    """Wire the endpoint's shared seams. `engine` is the scripted `_stream_hop`;
    `build_result` (when given) is what the mocked `build_context` returns."""
    seq = {"n": 0}

    async def _next_seq(db, cid):
        seq["n"] += 1
        return seq["n"]

    async def _get_trip(trip_id, user_id, db):
        return trip

    async def _refresh_state(t, db):
        pass

    async def _persist(*a, **k):
        pass

    monkeypatch.setattr(router.settings, "harness_mode", "agentic")
    monkeypatch.setattr(router, "_next_seq", _next_seq)
    monkeypatch.setattr(router, "get_trip", _get_trip)
    monkeypatch.setattr(router, "refresh_state", _refresh_state)
    monkeypatch.setattr(router, "_persist_tool_turn", _persist)
    if engine is not None:
        monkeypatch.setattr(router, "_stream_hop", engine)
    if build_result is not None:
        async def _build_context(**kwargs):
            return build_result
        monkeypatch.setattr(router, "build_context", _build_context)


# ---------------------------------------------------------------------------
# Gate 1 — the endpoint refuses a closed conversation before touching the engine
# ---------------------------------------------------------------------------

class TestClosedConversationRefusalAtEndpoint:
    async def test_already_closed_refuses_before_build_context(self, monkeypatch):
        # A conversation closed on a prior turn: the guard fires before build_context,
        # so no context is assembled, no engine hop runs, and no user row is persisted.
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), user_id=uuid.uuid4(), trip_id=uuid.uuid4(),
            context_state="closed",
        )
        trip = types.SimpleNamespace(id=conv.trip_id)
        user = types.SimpleNamespace(id=conv.user_id)
        db = _FakeDB(conv)

        touched = {"build": False, "engine": False}

        async def _boom_build(**k):
            touched["build"] = True
            raise AssertionError("build_context must not run for a closed conversation")

        async def _boom_engine(messages, *, tools):
            touched["engine"] = True
            yield {}

        _install_common(monkeypatch, trip=trip)
        monkeypatch.setattr(router, "build_context", _boom_build)
        monkeypatch.setattr(router, "_stream_hop", _boom_engine)

        resp = await router.chat_endpoint(
            {"conversation_id": str(conv.id), "message": "any more spots?"},
            user=user, db=db,
        )
        events = await _drain(resp)

        assert [e["type"] for e in events] == ["conversation_closed", "token", "done"]
        assert events[0]["message"] == router._CONVERSATION_CLOSED_MESSAGE
        assert touched == {"build": False, "engine": False}
        assert db.added == []  # no user/assistant message persisted — the turn never happened

    async def test_hard_budget_closes_and_refuses_this_turn(self, monkeypatch):
        # An open conversation whose assembled context this turn exceeds the hard stop:
        # the guard closes it, persists context_state, and refuses — engine untouched,
        # user message never persisted (the persist step is past the guard).
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), user_id=uuid.uuid4(), trip_id=uuid.uuid4(),
            context_state="ok",
        )
        trip = types.SimpleNamespace(id=conv.trip_id)
        user = types.SimpleNamespace(id=conv.user_id)
        db = _FakeDB(conv)

        # One oversized message → estimate (chars/4) sits above the hard threshold.
        huge = [{"role": "user", "content": "x" * ((CONTEXT_HARD_TOKENS + 500) * 4)}]
        build_result = _build_result(huge, [_candidate(str(uuid.uuid4()))])

        engine_hit = {"v": False}

        async def _boom_engine(messages, *, tools):
            engine_hit["v"] = True
            yield {}

        _install_common(monkeypatch, trip=trip, build_result=build_result)
        monkeypatch.setattr(router, "_stream_hop", _boom_engine)

        resp = await router.chat_endpoint(
            {"conversation_id": str(conv.id), "message": "keep going"},
            user=user, db=db,
        )
        events = await _drain(resp)

        assert [e["type"] for e in events] == ["conversation_closed", "token", "done"]
        assert conv.context_state == "closed"     # persisted for future turns
        assert engine_hit["v"] is False           # never sent above the prefill budget
        assert db.added == []                     # user turn not persisted


# ---------------------------------------------------------------------------
# Gate 2 — a full agentic turn touches no HERE surface
# ---------------------------------------------------------------------------

class TestNoHereThroughAgenticTurn:
    async def test_full_turn_engine_tool_answer_touches_no_here(self, monkeypatch):
        # Drive a real turn: an engine hop asks for get_spot, the REAL execute_tool
        # runs (active-set short-circuit → pure in-memory, no DB), then the answer hop
        # streams prose. Every HERE surface is booby-trapped; the turn must complete
        # without tripping one.
        spot_a = str(uuid.uuid4())
        candidates = [
            _candidate(spot_a, name="Yakima River"),
            _candidate(str(uuid.uuid4()), name="Naches River"),
            _candidate(str(uuid.uuid4()), name="Cle Elum River"),
        ]
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), user_id=uuid.uuid4(), trip_id=uuid.uuid4(),
            context_state="ok", excluded_spot_ids=[],
            pending_filter_update=None, session_candidates=None, last_active=None,
        )
        trip = types.SimpleNamespace(id=conv.trip_id)
        user = types.SimpleNamespace(id=conv.user_id)
        db = _FakeDB(conv)

        # spot_a is a frozen top-3 spot → in the active set → get_spot short-circuits.
        scripts = [
            [_sentinel(tool_calls=[_tool_call("get_spot", {"spot_id": spot_a})])],
            ["The Yakima is your best bet right now.", _sentinel()],
        ]
        engine, estate = _scripted_hop(scripts)
        build_result = _build_result([{"role": "user", "content": "tell me about the Yakima"}], candidates)

        _install_common(monkeypatch, trip=trip, build_result=build_result, engine=engine)
        # NOTE: execute_tool is deliberately NOT mocked — the real tool runs.

        # Booby-trap every HERE call surface reachable from a turn.
        from conditions import here_budget

        def _no_here(*a, **k):
            raise AssertionError("a turn must never touch HERE (spec §11.1)")

        async def _no_here_async(*a, **k):
            raise AssertionError("a turn must never touch HERE (spec §11.1)")

        monkeypatch.setattr(router, "_geocode_location", _no_here_async)
        monkeypatch.setattr(here_budget, "reserve", _no_here_async)

        resp = await router.chat_endpoint(
            {"conversation_id": str(conv.id), "message": "tell me about the Yakima"},
            user=user, db=db,
        )
        events = await _drain(resp)

        # The turn ran to completion: a tool hop happened, then prose, then done.
        assert estate["i"] == 2
        tokens = [e["content"] for e in events if e["type"] == "token"]
        assert "".join(tokens) == "The Yakima is your best bet right now."
        assert events[-1]["type"] == "done"
        # A "Looking up spot details…" status proves the real get_spot hop fired.
        labels = [e.get("label") for e in events if e["type"] == "tool_status"]
        assert "Looking up spot details…" in labels

    def test_catalog_has_no_here_or_pipeline_tool(self):
        # Structural half of the guarantee: the loop's catalog exposes only the three
        # HERE-free read tools — no geocoding / find_spots / re-ranking tool exists to
        # be called, and the tool module carries no HERE call surface.
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        assert names == {"get_spot", "search_notes_by_text", "compare_spots"}

        import inspect
        from chat import tools as tools_mod

        src = inspect.getsource(tools_mod)
        assert "hereapi.com" not in src
        assert "import httpx" not in src
        assert "from conditions.routing" not in src


# ---------------------------------------------------------------------------
# Gate 3 — exclusion: honored in the loop; NOT honored on a pipeline re-run
# ---------------------------------------------------------------------------

class TestExcludedResurfaceCorner:
    async def test_loop_get_spot_never_resurfaces_an_excluded_spot(self):
        # The in-loop guarantee: get_spot on an excluded spot returns a "set aside"
        # pointer without any DB read (a db that would raise proves it never fetched).
        class _BoomDB:
            async def execute(self, *a, **k):
                raise AssertionError("excluded get_spot must short-circuit before any DB read")

        excl = str(uuid.uuid4())
        full, digest = await execute_tool(
            "get_spot", {"spot_id": excl},
            conversation_id="c1", db=_BoomDB(),
            candidates=[_candidate(excl)], active_ids=set(), excluded_ids={excl},
        )
        assert full["status"] == "set_aside"
        assert "set aside" in digest

    async def test_confirm_filter_rerun_hands_pipeline_the_excluded_ids(self, monkeypatch):
        # The former orthogonal gap is fixed: confirm-filter's Yes-path re-runs
        # build_context(force_rerun=True), and the pipeline now filters
        # excluded_spot_ids (see chat.context_builder._filter_excluded, unit-tested in
        # test_context_builder). At the endpoint seam we assert the re-run is handed the
        # conversation carrying the excluded ids, so exclusion CAN be honored. HERE-free
        # (build_context mocked; key != departure_location so no geocode).
        excl = str(uuid.uuid4())
        conv = types.SimpleNamespace(
            id=uuid.uuid4(), user_id=uuid.uuid4(), trip_id=uuid.uuid4(),
            excluded_spot_ids=[uuid.UUID(excl)],
            pending_filter_update={"key": "max_drive_minutes", "value": "120"},
            session_candidates={"candidates": []},
        )
        trip = types.SimpleNamespace(id=conv.trip_id, session_intake={})
        user = types.SimpleNamespace(id=conv.user_id)
        db = _FakeDB(conv)

        async def _get_trip(trip_id, user_id, db):
            return trip

        # The real pipeline drops the excluded spot; the mock returns what a filtered
        # re-run would (the excluded spot absent), and records the conversation it saw.
        seen = {}
        rerun = _build_result([], [_candidate(str(uuid.uuid4()))])

        async def _build_context(**kwargs):
            assert kwargs.get("force_rerun") is True
            seen["excluded"] = {str(e) for e in (kwargs["conversation"].excluded_spot_ids or [])}
            return rerun

        monkeypatch.setattr(router, "get_trip", _get_trip)
        monkeypatch.setattr(router, "build_context", _build_context)

        out = await router.confirm_filter_endpoint(
            {"conversation_id": str(conv.id), "confirm": True}, user=user, db=db,
        )

        assert out["result"] == "accepted"
        # The pipeline received the excluded id (so _filter_excluded can drop it)...
        assert excl in seen["excluded"]
        # ...and the excluded spot is not among the re-run's candidates.
        assert excl not in {c["spot_id"] for c in out["session_candidates"]}
        assert conv.pending_filter_update is None
