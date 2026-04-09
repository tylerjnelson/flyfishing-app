"""
Manual integration test for the map ingestion pipeline.

Runs as a standalone async script (not pytest) so it can:
  - Hit the real database
  - Produce an actual corrected-map WebP you can inspect

Usage (run as root so env + venv are accessible):
  sudo /opt/flyfish/venv/bin/python scripts/test_map_ingestion.py [/path/to/image.jpg]

If no image path is given, a synthetic map image is generated automatically.

Output:
  - Inserts a Note row in the DB (source_type='map')
  - Stores processed image under /data/uploads/<note_id>/original.webp
  - Copies it to /tmp/map_test_output.webp for easy inspection
  - Prints the note ID so you can query it in psql
"""

import asyncio
import io
import os
import shutil
import sys
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Load env vars from the production env file before importing app modules
# ---------------------------------------------------------------------------

ENV_FILE = "/etc/flyfish/app.env"

def _load_env(path: str) -> None:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

_load_env(ENV_FILE)

# Add backend to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Imports (after env is loaded)
# ---------------------------------------------------------------------------

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from db.connection import AsyncSessionLocal
from db.models import Note, User
from notes.map_extractor import correct_standalone_map
from notes.upload_handler import read_upload, store_upload, validate_and_encode
from sqlalchemy import select


# ---------------------------------------------------------------------------
# Synthetic map image generator
# ---------------------------------------------------------------------------

def make_synthetic_map(width: int = 800, height: int = 600) -> bytes:
    """
    Create a JPEG that resembles a hand-drawn fishing spot map:
      - White background
      - Wavy river line
      - Grid reference lines
      - Labelled landmarks (X marks, annotations)
    Provides enough contrast and dimension to pass the OpenCV quality gate.
    """
    img = Image.new("RGB", (width, height), color=(245, 240, 225))  # cream paper
    draw = ImageDraw.Draw(img)

    # River — wavy blue/grey line
    river_pts = []
    for x in range(0, width, 10):
        y = int(height * 0.5 + 60 * np.sin(x / 80.0) + 20 * np.sin(x / 30.0))
        river_pts.append((x, y))
    draw.line(river_pts, fill=(100, 140, 180), width=8)

    # Banks (parallel lines either side)
    upper = [(x, y - 18) for x, y in river_pts]
    lower = [(x, y + 18) for x, y in river_pts]
    draw.line(upper, fill=(80, 60, 40), width=2)
    draw.line(lower, fill=(80, 60, 40), width=2)

    # Pool markers (circles)
    for cx, cy in [(200, 300), (450, 280), (650, 330)]:
        draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], outline=(40, 40, 40), width=2)
        draw.line([cx - 5, cy - 5, cx + 5, cy + 5], fill=(40, 40, 40), width=2)
        draw.line([cx + 5, cy - 5, cx - 5, cy + 5], fill=(40, 40, 40), width=2)

    # Riffle markers (hatching)
    for x in range(320, 400, 12):
        draw.line([(x, 260), (x + 8, 310)], fill=(60, 60, 60), width=1)

    # Compass rose (top-right)
    cx, cy = width - 60, 60
    draw.line([(cx, cy - 25), (cx, cy + 25)], fill=(40, 40, 40), width=2)
    draw.line([(cx - 25, cy), (cx + 25, cy)], fill=(40, 40, 40), width=2)
    draw.text((cx - 4, cy - 38), "N", fill=(20, 20, 20))

    # Labels
    draw.text((30, 30), "Yakima R. — Cle Elum Section", fill=(20, 20, 20))
    draw.text((160, 310), "deep pool", fill=(30, 30, 80))
    draw.text((410, 265), "riffle", fill=(30, 30, 80))
    draw.text((610, 340), "seam", fill=(30, 30, 80))
    draw.text((30, height - 40), "Not to scale — April 2026", fill=(100, 100, 100))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(image_path: str | None) -> None:
    # 1. Get image bytes
    if image_path:
        print(f"Using image: {image_path}")
        source_bytes = Path(image_path).read_bytes()
    else:
        print("No image supplied — generating synthetic map…")
        source_bytes = make_synthetic_map()

    # 2. Validate and encode to WebP (same as the upload endpoint)
    print("Running validate_and_encode()…")
    webp_bytes = validate_and_encode(source_bytes)
    print(f"  → {len(webp_bytes):,} bytes WebP")

    # 3. Find a user to attach the note to
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        test_user_created = False
        if not user:
            print("No users found — creating temporary test user…")
            user = User(
                id=uuid.uuid4(),
                email="test-map-pipeline@flyfish.local",
                display_name="Map Pipeline Test",
            )
            db.add(user)
            await db.flush()
            test_user_created = True

        print(f"Using author: {user.email} ({user.id})")

        # 4. Create Note record
        note_id = uuid.uuid4()
        note = Note(
            id=note_id,
            author_id=user.id,
            source_type="map",
            content="",  # filled in by ingestion; left blank for this pipeline test
            processing_notes="awaiting_date_confirmation|awaiting_spot_confirmation",
        )
        db.add(note)
        await db.flush()

        # 5. Store the upload
        image_path_stored = store_upload(webp_bytes, str(note_id))
        note.image_path = image_path_stored
        db.add(note)
        await db.commit()
        print(f"Note created: {note_id}")
        print(f"Stored upload: {image_path_stored}")

    # 6. Run the map correction pipeline (outside the session, same as ingest_note_task)
    print("\nRunning correct_standalone_map()…")
    raw_bytes = read_upload(str(note_id))
    corrected_path, is_low_quality = correct_standalone_map(raw_bytes, str(note_id))
    print(f"  → corrected_path: {corrected_path}")
    print(f"  → is_low_quality: {is_low_quality}")

    # 7. Copy output to /tmp for inspection
    output_copy = "/tmp/map_test_output.webp"
    shutil.copy(corrected_path, output_copy)
    print(f"\nCorrected image copied to: {output_copy}")

    # 8. Update note with corrected path + processing flags
    flags = ["low_quality_scan"] if is_low_quality else []
    flags += ["awaiting_date_confirmation", "awaiting_spot_confirmation"]
    processing_notes = "|".join(flags)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Note).where(Note.id == note_id))
        note = result.scalar_one()
        note.image_path = corrected_path
        note.processing_notes = processing_notes
        db.add(note)
        await db.commit()

    # 9. Verify DB record
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Note).where(Note.id == note_id))
        saved = result.scalar_one()
        print(f"\n--- DB record ---")
        print(f"  id:               {saved.id}")
        print(f"  source_type:      {saved.source_type}")
        print(f"  image_path:       {saved.image_path}")
        print(f"  processing_notes: {saved.processing_notes}")
        print(f"  author_id:        {saved.author_id}")
        print(f"  created_at:       {saved.created_at}")

    print("\nPipeline test complete.")
    print(f"  Inspect image : {output_copy}")
    print(f"  Note ID       : {note_id}")
    print(f"\nCleanup SQL:")
    print(f"  DELETE FROM notes WHERE id = '{note_id}';")
    if test_user_created:
        print(f"  DELETE FROM users WHERE id = '{user.id}';")


if __name__ == "__main__":
    img_arg = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(run(img_arg))
