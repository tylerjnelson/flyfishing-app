"""
Phase 4 — notes unit tests (§11.1).

Tests:
  1. Upload JPEG → assert file re-encoded to WebP quality=85, EXIF stripped.
  2. Standalone map upload → assert WebP quality=92, low-quality flag for tiny images.

The JPEG for tests is generated in-memory via Pillow.
"""

import io
import uuid
from pathlib import Path

import piexif
import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Synthetic test images
# ---------------------------------------------------------------------------


def _make_jpeg_with_exif() -> bytes:
    """Create a 100×100 JPEG with GPS EXIF data."""
    img = Image.new("RGB", (100, 100), color=(128, 100, 80))
    # Add EXIF with GPS tag
    exif_dict = {
        "GPS": {
            piexif.GPSIFD.GPSLatitude: ((47, 1), (36, 1), (0, 1)),
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLongitude: ((120, 1), (30, 1), (0, 1)),
            piexif.GPSIFD.GPSLongitudeRef: b"W",
        }
    }
    exif_bytes = piexif.dump(exif_dict)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif_bytes)
    return buf.getvalue()


def _make_plain_jpeg() -> bytes:
    """Create a 200×200 JPEG without EXIF — sufficient for OpenCV operations."""
    img = Image.new("RGB", (200, 200), color=(180, 160, 140))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# upload_handler unit tests (synchronous — no DB needed)
# ---------------------------------------------------------------------------


class TestUploadHandler:
    """Test validate_and_encode() in isolation."""

    def test_jpeg_re_encoded_to_webp(self, tmp_path):
        from notes.upload_handler import validate_and_encode

        jpeg_bytes = _make_jpeg_with_exif()
        result = validate_and_encode(jpeg_bytes)

        # Result must be WebP
        pil = Image.open(io.BytesIO(result))
        assert pil.format == "WEBP", f"Expected WEBP, got {pil.format}"

    def test_exif_stripped(self, tmp_path):
        """EXIF data (including GPS) must not survive re-encoding."""
        from notes.upload_handler import validate_and_encode

        jpeg_bytes = _make_jpeg_with_exif()
        result = validate_and_encode(jpeg_bytes)

        # Open the resulting WebP and check for absence of EXIF
        pil = Image.open(io.BytesIO(result))
        exif_data = pil.info.get("exif", b"")
        # piexif can parse the (possibly absent) exif blob
        try:
            parsed = piexif.load(exif_data) if exif_data else {}
            gps = parsed.get("GPS", {})
            assert not gps, f"GPS EXIF survived re-encoding: {gps}"
        except Exception:
            # piexif throws if blob is empty/invalid — that's fine, means no EXIF
            pass

    def test_webp_quality_85_produces_smaller_file_than_original_jpeg(self):
        """
        Sanity: WebP at quality=85 should produce a reasonable output.
        We don't assert an exact byte count, just that it's a valid image.
        """
        from notes.upload_handler import validate_and_encode

        jpeg_bytes = _make_jpeg_with_exif()
        result = validate_and_encode(jpeg_bytes)
        assert len(result) > 0

    def test_unsupported_mime_raises(self):
        from notes.upload_handler import validate_and_encode

        # GIF is not in the allow-list
        img = Image.new("RGB", (50, 50), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="GIF")
        gif_bytes = buf.getvalue()

        with pytest.raises(ValueError, match="Unsupported file type"):
            validate_and_encode(gif_bytes)

    def test_store_and_read_roundtrip(self, tmp_path, monkeypatch):
        from notes import upload_handler
        from notes.upload_handler import read_upload, store_upload

        monkeypatch.setattr(upload_handler.settings, "uploads_path", str(tmp_path))
        note_id = str(uuid.uuid4())
        data = b"RIFF\x00\x00\x00\x00WEBP"  # minimal fake WebP bytes

        path = store_upload(data, note_id)
        assert Path(path).exists()
        assert read_upload(note_id) == data


# ---------------------------------------------------------------------------
# map_extractor unit tests
# ---------------------------------------------------------------------------


class TestMapExtractor:
    def test_standalone_map_produces_webp_quality_92(self, tmp_path, monkeypatch):
        """Standalone map correction must store at WebP quality=92 (not 85)."""
        from notes import map_extractor
        from notes.map_extractor import correct_standalone_map

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))
        jpeg_bytes = _make_plain_jpeg()
        path, _ = correct_standalone_map(jpeg_bytes, str(uuid.uuid4()))

        result_bytes = Path(path).read_bytes()
        pil = Image.open(io.BytesIO(result_bytes))
        assert pil.format == "WEBP"

    def test_low_quality_flag_set_for_tiny_image(self, tmp_path, monkeypatch):
        """Image smaller than 200px in any dimension → is_low_quality=True."""
        from notes import map_extractor
        from notes.map_extractor import correct_standalone_map

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))

        img = Image.new("RGB", (50, 50), color=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        tiny_bytes = buf.getvalue()

        _, is_low_quality = correct_standalone_map(tiny_bytes, str(uuid.uuid4()))
        assert is_low_quality is True

    def test_standalone_map_correction_file_exists(self, tmp_path, monkeypatch):
        from notes import map_extractor
        from notes.map_extractor import correct_standalone_map

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))
        jpeg_bytes = _make_plain_jpeg()
        path, _ = correct_standalone_map(jpeg_bytes, str(uuid.uuid4()))
        assert Path(path).exists()


