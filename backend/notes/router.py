"""
Notes API

POST   /api/notes/upload              — upload typed text or image note
GET    /api/notes                     — list notes (corpus browser)
GET    /api/notes/{note_id}           — single note detail + processing state
GET    /api/notes/{note_id}/image     — auth-gated image serving
GET    /api/notes/{note_id}/map       — auth-gated extracted map serving
PATCH  /api/notes/{note_id}/confirm-date  — confirm extracted date
PATCH  /api/notes/{note_id}/confirm-spot  — confirm or correct spot resolution
"""

import logging
from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from auth.middleware import get_current_user
from db.connection import get_db
from db.models import User
from notes import ingestion as ingestion_mod
from notes import service
from notes.spot_resolver import apply_correction
from notes.upload_handler import upload_path, validate_and_encode

log = logging.getLogger(__name__)

router = APIRouter()

_VALID_SOURCE_TYPES = {"typed", "handwritten", "map"}


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------


def _note_summary(note) -> dict:
    state = service.parse_processing_notes(note.processing_notes)
    return {
        "id": str(note.id),
        "source_type": note.source_type,
        "note_date": note.note_date.isoformat() if note.note_date else None,
        "outcome": note.outcome,
        "species": note.species or [],
        "flies": note.flies or [],
        "spot_id": str(note.spot_id) if note.spot_id else None,
        "parent_note_id": str(note.parent_note_id) if note.parent_note_id else None,
        "has_image": note.image_path is not None,
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "pending_flags": state["flags"],
    }


def _note_detail(note) -> dict:
    base = _note_summary(note)
    state = service.parse_processing_notes(note.processing_notes)
    base.update(
        {
            "content": note.content,
            "negative_reason": note.negative_reason,
            "approx_cfs": note.approx_cfs,
            "approx_temp": float(note.approx_temp) if note.approx_temp is not None else None,
            "time_of_day": note.time_of_day,
            "trip_id": str(note.trip_id) if note.trip_id else None,
            "processing_notes_raw": note.processing_notes,
            "spot_resolution": state.get("spot_resolution"),
        }
    )
    return base


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post("/upload", status_code=201)
async def upload_note(
    background_tasks: BackgroundTasks,
    source_type: str = Form(...),
    content: str | None = Form(None),
    spot_id: str | None = Form(None),
    trip_id: str | None = Form(None),
    file: UploadFile | None = File(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if source_type not in _VALID_SOURCE_TYPES:
        raise HTTPException(400, f"source_type must be one of: {', '.join(_VALID_SOURCE_TYPES)}")

    if source_type == "typed":
        if not content:
            raise HTTPException(400, "content is required for typed notes")
        note = await service.create_note(
            db,
            author_id=current_user.id,
            source_type="typed",
            content=content,
            spot_id=UUID(spot_id) if spot_id else None,
            trip_id=UUID(trip_id) if trip_id else None,
        )
        await db.commit()
        await db.refresh(note)
        background_tasks.add_task(
            ingestion_mod.ingest_note_task,
            note.id,
            "typed",
            current_user.id,
        )
        return {"note_id": str(note.id), "status": "processing"}

    # Image upload (handwritten or standalone map)
    if not file:
        raise HTTPException(400, "file is required for image notes")

    raw_bytes = await file.read()
    try:
        webp_bytes = validate_and_encode(raw_bytes)
    except ValueError as exc:
        raise HTTPException(422, str(exc))

    # Create initial note record (content filled in by background task)
    note = await service.create_note(
        db,
        author_id=current_user.id,
        source_type=source_type,
        content="",
        spot_id=UUID(spot_id) if spot_id else None,
        trip_id=UUID(trip_id) if trip_id else None,
    )
    await db.flush()

    # Store processed image under the note's ID
    from notes.upload_handler import store_upload as _store

    image_path = _store(webp_bytes, str(note.id))
    note.image_path = image_path
    db.add(note)
    await db.commit()
    await db.refresh(note)

    background_tasks.add_task(
        ingestion_mod.ingest_note_task,
        note.id,
        source_type,
        current_user.id,
    )
    return {"note_id": str(note.id), "status": "processing"}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("")
async def list_notes(
    spot_id: str | None = Query(None),
    source_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    notes = await service.list_notes(
        db,
        author_id=current_user.id,
        spot_id=UUID(spot_id) if spot_id else None,
        source_type=source_type,
        limit=limit,
        offset=offset,
    )
    return {"notes": [_note_summary(n) for n in notes], "count": len(notes)}


@router.get("/{note_id}")
async def get_note(
    note_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await service.get_note(note_id, db)
    if not note:
        raise HTTPException(404, "Note not found")
    if note.author_id != current_user.id:
        raise HTTPException(403, "Forbidden")
    return _note_detail(note)


@router.get("/{note_id}/image")
async def serve_image(
    note_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await service.get_note(note_id, db)
    if not note:
        raise HTTPException(404, "Note not found")
    if note.author_id != current_user.id:
        raise HTTPException(403, "Forbidden")
    if not note.image_path:
        raise HTTPException(404, "No image for this note")
    path = Path(note.image_path)
    if not path.exists():
        raise HTTPException(404, "Image file not found on disk")
    return FileResponse(path, media_type="image/webp")


@router.get("/{note_id}/map")
async def serve_map(
    note_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Serve the extracted map for a handwritten note (its first child map record),
    or serve the image directly if this note is itself a map note.
    """
    from sqlalchemy import select as sa_select

    from db.models import Note as NoteModel

    note = await service.get_note(note_id, db)
    if not note:
        raise HTTPException(404, "Note not found")
    if note.author_id != current_user.id:
        raise HTTPException(403, "Forbidden")

    if note.source_type == "map":
        target = note
    else:
        # Find first child map note
        result = await db.execute(
            sa_select(NoteModel)
            .where(NoteModel.parent_note_id == note_id)
            .where(NoteModel.source_type == "map")
            .limit(1)
        )
        target = result.scalar_one_or_none()
        if not target:
            raise HTTPException(404, "No extracted map for this note")

    if not target.image_path:
        raise HTTPException(404, "No image for map note")
    path = Path(target.image_path)
    if not path.exists():
        raise HTTPException(404, "Map image file not found on disk")
    return FileResponse(path, media_type="image/webp")


# ---------------------------------------------------------------------------
# Confirmation endpoints
# ---------------------------------------------------------------------------


@router.patch("/{note_id}/confirm-date")
async def confirm_date(
    note_id: UUID,
    confirmed_date: date,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    note = await service.get_note(note_id, db)
    if not note:
        raise HTTPException(404, "Note not found")
    if note.author_id != current_user.id:
        raise HTTPException(403, "Forbidden")

    updated = await service.confirm_date(note_id, confirmed_date, db)
    await db.commit()
    return {"note_id": str(note_id), "note_date": confirmed_date.isoformat()}


@router.patch("/{note_id}/confirm-spot")
async def confirm_spot(
    note_id: UUID,
    spot_id: str,
    is_correction: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Confirm or correct the spot for a note.
    If is_correction=True, appends the extracted location_string to the spot's
    aliases and re-generates name_embedding (D6 alias update).
    """
    note = await service.get_note(note_id, db)
    if not note:
        raise HTTPException(404, "Note not found")
    if note.author_id != current_user.id:
        raise HTTPException(403, "Forbidden")

    if is_correction:
        # Retrieve the stored location_string from processing_notes
        state = service.parse_processing_notes(note.processing_notes)
        loc_str = ""
        if state.get("spot_resolution"):
            loc_str = state["spot_resolution"].get("location_string", "")
        if loc_str:
            await apply_correction(spot_id, loc_str, note_id, db)

    updated = await service.confirm_spot(note_id, UUID(spot_id), db)
    await db.commit()
    return {"note_id": str(note_id), "spot_id": spot_id}
