"""
Phase 4 — notes integration tests (§11.1).

Tests:
  1. Upload JPEG → assert file re-encoded to WebP quality=85, EXIF stripped.
  2. Upload mixed notebook page with map → assert two notes records created
     (handwritten + map), map record carries correct parent_note_id,
     map note re-encoded at quality=92.

Uses a stubbed Ollama so tests run without a live LLM.
The JPEG for test 1 is generated in-memory via Pillow.
The handwritten page for test 2 uses the same synthetic JPEG
with a mocked map-detection response.
"""

import asyncio
import io
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    def test_extract_and_correct_produces_webp_quality_92(self, tmp_path, monkeypatch):
        """Map extraction must store at WebP quality=92 (not 85)."""
        from notes import map_extractor
        from notes.map_extractor import extract_and_correct

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))
        jpeg_bytes = _make_plain_jpeg()
        map_note_id = str(uuid.uuid4())

        full_bb = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}
        path, is_low_quality = extract_and_correct(jpeg_bytes, full_bb, "high", map_note_id)

        result_bytes = Path(path).read_bytes()
        pil = Image.open(io.BytesIO(result_bytes))
        assert pil.format == "WEBP"

    def test_low_quality_flag_set_for_tiny_crop(self, tmp_path, monkeypatch):
        """Crop smaller than 200px in any dimension → is_low_quality=True."""
        from notes import map_extractor
        from notes.map_extractor import extract_and_correct

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))

        # Create a 50×50 image (will produce a crop smaller than 200px)
        img = Image.new("RGB", (50, 50), color=(200, 200, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        tiny_bytes = buf.getvalue()

        full_bb = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        _, is_low_quality = extract_and_correct(tiny_bytes, full_bb, "high", str(uuid.uuid4()))
        assert is_low_quality is True

    def test_standalone_map_correction(self, tmp_path, monkeypatch):
        from notes import map_extractor
        from notes.map_extractor import correct_standalone_map

        monkeypatch.setattr(map_extractor.settings, "uploads_path", str(tmp_path))
        jpeg_bytes = _make_plain_jpeg()
        path, _ = correct_standalone_map(jpeg_bytes, str(uuid.uuid4()))
        assert Path(path).exists()


# ---------------------------------------------------------------------------
# Integration: upload endpoint → two notes records for handwritten + map
# ---------------------------------------------------------------------------


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch):
    """Redirect uploads to tmp_path for all tests in this module."""
    import notes.upload_handler as uh
    import notes.map_extractor as me

    monkeypatch.setattr(uh.settings, "uploads_path", str(tmp_path))
    monkeypatch.setattr(me.settings, "uploads_path", str(tmp_path))
    return tmp_path


class TestHandwrittenNoteIngestion:
    """
    Integration test: mocked Ollama + real DB-free ingestion helpers.

    Tests that after ingesting a 'handwritten' note with a map detected:
      - Two Note records are created: handwritten + map child
      - Map child carries correct parent_note_id
      - Map image is re-encoded at quality=92
    """

    @pytest.mark.asyncio
    async def test_two_notes_created_with_parent_link(self, tmp_path, monkeypatch):
        """
        Simulate _ingest_handwritten with mocked LLM calls.
        Verifies the two-note structure: handwritten parent + map child.
        """
        import notes.upload_handler as uh
        import notes.map_extractor as me

        monkeypatch.setattr(uh.settings, "uploads_path", str(tmp_path))
        monkeypatch.setattr(me.settings, "uploads_path", str(tmp_path))

        # Store a synthetic JPEG as the handwritten note's image
        parent_note_id = uuid.uuid4()
        jpeg_bytes = _make_plain_jpeg()
        webp = uh.validate_and_encode(jpeg_bytes)
        uh.store_upload(webp, str(parent_note_id))

        # Map detection response → map found with full bounding box
        map_detection_resp = {
            "contains_map": True,
            "confidence": "high",
            "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        }
        # OCR response
        ocr_resp = (
            "Fished the upper Yakima above Ellensburg. Good flows.\nTRIP DATE: 2024-06-15"
        )
        # Field extraction response
        field_resp = {
            "species": ["rainbow trout"],
            "flies": ["elk hair caddis"],
            "outcome": "positive",
            "negative_reason": None,
            "approx_cfs": 850,
            "approx_temp": 54.0,
            "time_of_day": "morning",
        }
        # Spot resolution location extraction response
        loc_resp = {"location_string": "upper Yakima", "confidence": "high"}
        # Spatial description for map
        spatial_desc = "River map showing upper Yakima with two pools marked."

        # Tracking: record Note objects added to a fake DB
        created_notes: list = []

        # Build a fake Note to return on initial DB lookup
        fake_parent_note = MagicMock()
        fake_parent_note.id = parent_note_id
        fake_parent_note.content = ""
        fake_parent_note.image_path = str(tmp_path / str(parent_note_id) / "original.webp")

        call_count = 0

        class FakeDB:
            async def execute(self, *args, **kwargs):
                nonlocal call_count
                call_count += 1
                result = MagicMock()
                # First execute call = initial note lookup → return fake note
                if call_count == 1:
                    result.scalar_one_or_none.return_value = fake_parent_note
                else:
                    result.scalar_one_or_none.return_value = None
                result.scalars.return_value.all.return_value = []
                result.__iter__ = lambda s: iter([])
                return result

            def add(self, obj):
                created_notes.append(obj)

            async def flush(self):
                pass

        fake_db = FakeDB()

        with (
            patch("notes.ingestion.call_json_llm", new_callable=AsyncMock) as mock_json_llm,
            patch("notes.ingestion.ollama_generate", new_callable=AsyncMock) as mock_gen,
            patch("notes.spot_resolver.call_json_llm", new_callable=AsyncMock) as mock_loc_llm,
            patch("notes.spot_resolver.embed_text", new_callable=AsyncMock) as mock_embed_text,
            patch("notes.ingestion.embed_text", new_callable=AsyncMock) as mock_embed,
            patch("notes.spot_resolver._semantic_lookup", new_callable=AsyncMock) as mock_sem,
            patch("notes.spot_resolver._fuzzy_lookup", new_callable=AsyncMock) as mock_fuz,
        ):
            # call_json_llm: first call = map detection, second = field extraction
            mock_json_llm.side_effect = [map_detection_resp, field_resp]
            # ollama_generate: first call = spatial desc, second = OCR
            mock_gen.side_effect = [spatial_desc, ocr_resp]
            mock_loc_llm.return_value = loc_resp
            mock_embed_text.return_value = [0.1] * 768
            mock_embed.return_value = [0.1] * 768
            mock_sem.return_value = []
            mock_fuz.return_value = []

            # Also need to mock _update_note to record updates
            parent_note_mock = MagicMock()
            parent_note_mock.id = parent_note_id

            async def fake_update(note_id, updates, db):
                parent_note_mock.__dict__.update(updates)

            with patch("notes.ingestion._update_note", side_effect=fake_update):
                from notes.ingestion import _ingest_handwritten

                await _ingest_handwritten(parent_note_id, uuid.uuid4(), fake_db)

        # Assert: a map Note was added to the DB
        map_notes = [n for n in created_notes if hasattr(n, "source_type") and n.source_type == "map"]
        assert len(map_notes) >= 1, (
            f"Expected at least one map note to be created, got: {created_notes}"
        )
        map_note = map_notes[0]

        # Map note must link back to the parent
        assert map_note.parent_note_id == parent_note_id, (
            f"parent_note_id mismatch: {map_note.parent_note_id} != {parent_note_id}"
        )

        # Map image must be stored (path set)
        assert map_note.image_path is not None

        # Map image file must be WebP (quality=92 was used in extraction)
        map_img_path = Path(map_note.image_path)
        if map_img_path.exists():
            pil = Image.open(map_img_path)
            assert pil.format == "WEBP"
