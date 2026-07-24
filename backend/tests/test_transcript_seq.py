"""
Phase 2 validation — transcript ordering comes from `seq`, not `created_at`.

The load-bearing property (plan Phase 2): a single agentic turn appends an
ordered run of rows — user -> assistant[tool_calls] -> tool[digest] ->
assistant[final] — that all commit in one transaction and therefore share a
byte-identical `created_at` (Postgres now() = transaction-start time). Ordering
by `created_at` is then non-deterministic; only `seq` recovers the true order.

These tests persist such a turn (rows inserted deliberately out of order, with
an *identical* created_at) and assert the rebuilt transcript is correct when
read `ORDER BY seq`, and that `_next_seq` hands out the right next ordinal.

Runs on the in-memory SQLite harness. Conversation/Message carry Postgres-only
column types (JSONB, ARRAY); the @compiles shims below teach the SQLite dialect
to render them as JSON so create_all succeeds. The shims only fire for the
sqlite dialect, so they cannot affect production (Postgres) DDL.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import ARRAY, StaticPool, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from chat.router import _next_seq
from db.models import Base, Conversation, Message


@compiles(JSONB, "sqlite")
def _render_jsonb_as_json_on_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _render_array_as_json_on_sqlite(element, compiler, **kw):  # noqa: ANN001
    return "JSON"


@pytest.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [Conversation.__table__, Message.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=tables)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=tables)
    await engine.dispose()


@pytest.fixture
async def conversation(db):
    conv = Conversation(id=uuid.uuid4())  # user_id/trip_id left NULL (FK off on sqlite)
    db.add(conv)
    await db.commit()
    return conv


# The four rows of one agentic turn, in transcript order. Kept as data so a
# test can insert them shuffled and still know the expected result.
def _turn_rows(conversation_id, created_at):
    return [
        dict(seq=0, role="user", content="where should I fish today?"),
        dict(
            seq=1,
            role="assistant",
            content="",
            tool_calls=[{"function": {"name": "get_spot", "arguments": {"spot_id": "yak-1"}}}],
        ),
        dict(seq=2, role="tool", tool_name="get_spot", content="", digest='{"cfs": 1200}'),
        dict(seq=3, role="assistant", content="Try the Yakima — flows look good."),
    ]


async def _insert(db, conversation_id, row, created_at):
    db.add(Message(id=uuid.uuid4(), conversation_id=conversation_id, created_at=created_at, **row))


class TestTranscriptSeqOrdering:
    async def test_multihop_turn_rebuilds_in_seq_order_despite_identical_created_at(
        self, db, conversation
    ):
        # Every row of the turn shares ONE created_at — the whole point.
        shared_ts = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        rows = _turn_rows(conversation.id, shared_ts)

        # Insert deliberately OUT of transcript order so a correct read cannot be
        # an accident of insertion order (or of created_at, which is identical).
        for row in [rows[3], rows[1], rows[0], rows[2]]:
            await _insert(db, conversation.id, row, shared_ts)
        await db.commit()

        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.seq)
        )
        got = list(result.scalars().all())

        assert [m.seq for m in got] == [0, 1, 2, 3]
        assert [m.role for m in got] == ["user", "assistant", "tool", "assistant"]
        # created_at is useless as an ordering key here: all identical.
        assert len({m.created_at for m in got}) == 1

    async def test_tool_columns_round_trip(self, db, conversation):
        shared_ts = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        rows = _turn_rows(conversation.id, shared_ts)
        for row in rows:
            await _insert(db, conversation.id, row, shared_ts)
        await db.commit()

        result = await db.execute(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.seq)
        )
        got = list(result.scalars().all())

        assistant_call = got[1]
        assert assistant_call.tool_calls[0]["function"]["name"] == "get_spot"
        tool_row = got[2]
        assert tool_row.role == "tool"
        assert tool_row.tool_name == "get_spot"
        assert tool_row.digest == '{"cfs": 1200}'


class TestNextSeq:
    async def test_empty_conversation_starts_at_zero(self, db, conversation):
        assert await _next_seq(db, conversation.id) == 0

    async def test_next_seq_is_max_plus_one(self, db, conversation):
        shared_ts = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        for row in _turn_rows(conversation.id, shared_ts):
            await _insert(db, conversation.id, row, shared_ts)
        await db.commit()
        # highest seq is 3 -> next append is 4
        assert await _next_seq(db, conversation.id) == 4

    async def test_next_seq_isolated_per_conversation(self, db, conversation):
        shared_ts = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)
        for row in _turn_rows(conversation.id, shared_ts):
            await _insert(db, conversation.id, row, shared_ts)
        await db.commit()

        other = Conversation(id=uuid.uuid4())
        db.add(other)
        await db.commit()
        # a different conversation's seq is independent
        assert await _next_seq(db, other.id) == 0
