"""
Upload hardening for note images.

Validates MIME type, strips EXIF, and re-encodes to WebP quality=85.
Stores the processed image to /data/uploads/{note_id}/original.webp.

pillow-heif is registered at app startup (main.py) which makes Pillow
able to open HEIC/HEIF files natively.
"""

import io
import logging
from pathlib import Path

import magic
from PIL import Image

from config import settings

log = logging.getLogger(__name__)

ALLOWED_MIMES = {
    "image/jpeg",
    "image/heic",
    "image/heif",
    "image/png",
    "image/webp",
}


def validate_and_encode(data: bytes) -> bytes:
    """
    Validate MIME type, strip EXIF, re-encode to WebP quality=85.

    Returns WebP bytes on success. Raises ValueError on unsupported MIME type.
    EXIF is stripped implicitly — Pillow does not copy EXIF when saving WebP
    unless exif= is explicitly passed.
    """
    mime = magic.from_buffer(data, mime=True)
    if mime not in ALLOWED_MIMES:
        raise ValueError(f"Unsupported file type: {mime}")

    img = Image.open(io.BytesIO(data))
    # Convert to RGB to ensure WebP compatibility (strips alpha channel too,
    # acceptable for scanned notebook pages).
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    # quality=85 for all uploads at this stage (pre-extraction default per §6.8).
    # exif= not passed → EXIF not included in output (GPS/timestamp stripped).
    img.save(buf, format="WEBP", quality=85)
    return buf.getvalue()


def store_upload(webp_bytes: bytes, note_id: str) -> str:
    """
    Write WebP bytes to /data/uploads/{note_id}/original.webp.
    Returns the stored path as a string.
    """
    dir_path = Path(settings.uploads_path) / note_id
    dir_path.mkdir(parents=True, exist_ok=True)
    dest = dir_path / "original.webp"
    dest.write_bytes(webp_bytes)
    log.info(
        "upload_stored",
        extra={"note_id": note_id, "bytes": len(webp_bytes), "path": str(dest)},
    )
    return str(dest)


def read_upload(note_id: str) -> bytes:
    """Read stored upload bytes for a given note_id."""
    path = Path(settings.uploads_path) / note_id / "original.webp"
    return path.read_bytes()


def upload_path(note_id: str) -> Path:
    """Return the Path for a note's stored image."""
    return Path(settings.uploads_path) / note_id / "original.webp"
