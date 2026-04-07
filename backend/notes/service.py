"""
Notes service — CRUD and query operations.
"""

import json
import logging
from datetime import date
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Note

log = logging.getLogger(__name__)


async def create_note(
    db: AsyncSession,
    author_id: UUID,
    source_type: str,
    content: str | None = None,
    image_path: str | None = None,
    spot_id: UUID | None = None,
    trip_id: UUID | None = None,
) -> Note:
    note = Note(
        author_id=author_id,
        source_type=source_type,
        content=content or "",
        image_path=image_path,
        spot_id=spot_id,
        trip_id=trip_id,
    )
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


async def get_note(note_id: UUID, db: AsyncSession) -> Note | None:
    result = await db.execute(select(Note).where(Note.id == note_id))
    return result.scalar_one_or_none()


async def list_notes(
    db: AsyncSession,
    author_id: UUID,
    spot_id: UUID | None = None,
    source_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Note]:
    q = select(Note).where(Note.author_id == author_id)
    if spot_id:
        q = q.where(Note.spot_id == spot_id)
    if source_type:
        q = q.where(Note.source_type == source_type)
    q = q.order_by(desc(Note.created_at)).limit(limit).offset(offset)
    result = await db.execute(q)
    return list(result.scalars().all())


async def confirm_date(
    note_id: UUID, confirmed_date: date, db: AsyncSession
) -> Note | None:
    """
    Set note_date on the note and propagate to any child map notes.
    Clears 'awaiting_date_confirmation' from processing_notes.
    """
    result = await db.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        return None

    note.note_date = confirmed_date
    note.processing_notes = _remove_flag(note.processing_notes, "awaiting_date_confirmation")
    db.add(note)

    # Propagate to child map notes
    children_result = await db.execute(
        select(Note).where(Note.parent_note_id == note_id)
    )
    for child in children_result.scalars().all():
        child.note_date = confirmed_date
        child.processing_notes = _remove_flag(
            child.processing_notes, "awaiting_date_confirmation"
        )
        db.add(child)

    await db.flush()
    await db.refresh(note)
    return note


async def confirm_spot(
    note_id: UUID, spot_id: UUID, db: AsyncSession
) -> Note | None:
    """
    Set spot_id on the note and clear awaiting_spot_confirmation flag.
    """
    result = await db.execute(select(Note).where(Note.id == note_id))
    note = result.scalar_one_or_none()
    if not note:
        return None

    note.spot_id = spot_id
    note.processing_notes = _remove_flag(note.processing_notes, "awaiting_spot_confirmation")
    # Remove the spot resolution JSON blob (second line of processing_notes if present)
    if note.processing_notes and "\n" in note.processing_notes:
        note.processing_notes = note.processing_notes.split("\n", 1)[0]
    db.add(note)
    await db.flush()
    await db.refresh(note)
    return note


def parse_processing_notes(processing_notes: str | None) -> dict:
    """
    Parse the pipe|newline processing_notes format into a structured dict.

    Returns:
    {
        "flags": [...],
        "spot_resolution": {...} | None,
    }
    """
    if not processing_notes:
        return {"flags": [], "spot_resolution": None}

    parts = processing_notes.split("\n", 1)
    flags = [f for f in parts[0].split("|") if f]
    spot_resolution = None
    if len(parts) > 1:
        try:
            spot_resolution = json.loads(parts[1])
        except (json.JSONDecodeError, ValueError):
            pass

    return {"flags": flags, "spot_resolution": spot_resolution}


def _remove_flag(processing_notes: str | None, flag: str) -> str:
    """Remove a flag from the pipe-separated flag portion of processing_notes."""
    if not processing_notes:
        return ""
    parts = processing_notes.split("\n", 1)
    flags = [f for f in parts[0].split("|") if f and f != flag]
    flag_str = "|".join(flags)
    if len(parts) > 1:
        return f"{flag_str}\n{parts[1]}"
    return flag_str
