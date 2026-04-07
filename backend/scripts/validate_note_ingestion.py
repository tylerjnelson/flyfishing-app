"""
validate_note_ingestion.py — end-to-end ingestion pipeline inspector.

Runs a real image through every stage of the notes ingestion pipeline, prints
each intermediate result, and saves corrected map images to an output directory
so you can inspect quality at each step before anything touches the database.

Usage:
    python -m scripts.validate_note_ingestion IMAGE [IMAGE ...]
    python -m scripts.validate_note_ingestion page.jpg --source-type map
    python -m scripts.validate_note_ingestion page.jpg --no-db --output-dir /tmp/val

Options:
    --source-type  handwritten|map  (default: handwritten)
    --no-db        skip spot resolution DB lookups (run without a DB connection)
    --output-dir   where to save re-encoded images and results JSON (default: /tmp/flyfish_val_<ts>)

Requires Ollama to be running.  DB is optional (--no-db skips spot resolution).
"""

import argparse
import asyncio
import base64
import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# ANSI colour helpers
# ---------------------------------------------------------------------------

_BOLD   = "\033[1m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_CYAN   = "\033[36m"
_RED    = "\033[31m"
_DIM    = "\033[2m"
_RESET  = "\033[0m"

def _h(text: str) -> str:
    return f"{_BOLD}{_CYAN}{text}{_RESET}"

def _ok(text: str) -> str:
    return f"{_GREEN}{text}{_RESET}"

def _warn(text: str) -> str:
    return f"{_YELLOW}{text}{_RESET}"

def _err(text: str) -> str:
    return f"{_RED}{text}{_RESET}"

def _dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}"

def _sep(title: str = "") -> None:
    if title:
        print(f"\n{_BOLD}{'─' * 4} {title} {'─' * (50 - len(title))}{_RESET}")
    else:
        print(f"{_DIM}{'─' * 60}{_RESET}")


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------

class _Timer:
    def __init__(self) -> None:
        self._start = time.time()

    def elapsed(self) -> str:
        return f"{_dim(f'({time.time() - self._start:.1f}s)')}"


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def run_upload_hardening(image_path: Path, output_dir: Path) -> tuple[bytes, dict]:
    """Stage 1: MIME validation, EXIF strip, WebP quality=85 re-encode."""
    from notes.upload_handler import validate_and_encode, store_upload

    raw = image_path.read_bytes()
    input_kb = len(raw) / 1024

    import magic
    detected_mime = magic.from_buffer(raw, mime=True)

    t = _Timer()
    webp_bytes = validate_and_encode(raw)
    output_kb = len(webp_bytes) / 1024

    # Save to output dir
    out_path = output_dir / f"{image_path.stem}_original.webp"
    out_path.write_bytes(webp_bytes)

    result = {
        "input_path": str(image_path),
        "input_size_kb": round(input_kb, 1),
        "detected_mime": detected_mime,
        "output_path": str(out_path),
        "output_size_kb": round(output_kb, 1),
        "compression_ratio": round(input_kb / output_kb, 2) if output_kb else 0,
    }

    _sep("STAGE 1 — UPLOAD HARDENING")
    print(f"  Input:  {image_path.name}  {_dim(f'({input_kb:.0f} KB, {detected_mime})')}")
    print(f"  Output: {out_path.name}  {_dim(f'({output_kb:.0f} KB)')}")
    ratio = result["compression_ratio"]
    ratio_str = f"{ratio:.2f}×" if ratio >= 1 else f"1/{1/ratio:.2f}×"
    print(f"  Size ratio: {ratio_str}  |  EXIF: {_ok('stripped')}  |  Quality: 85  {t.elapsed()}")

    return webp_bytes, result


async def run_map_detection(webp_bytes: bytes) -> tuple[dict, str]:
    """Stage 2: Ask Vision model whether the image contains a map."""
    from llm.client import VISION_MODEL, call_json_llm
    from prompts.registry import MAP_DETECTION_PROMPT

    b64 = base64.b64encode(webp_bytes).decode("utf-8")
    t = _Timer()
    detection = await call_json_llm(
        MAP_DETECTION_PROMPT,
        VISION_MODEL,
        {"contains_map": False, "confidence": "low", "bounding_box": None},
        images=[b64],
    )

    _sep("STAGE 2 — MAP DETECTION")
    contains = detection.get("contains_map", False)
    confidence = detection.get("confidence", "?")
    bb = detection.get("bounding_box")

    marker = _ok("YES") if contains else _dim("no")
    conf_color = _ok if confidence == "high" else (_warn if confidence == "medium" else _dim)
    print(f"  contains_map: {marker}    confidence: {conf_color(confidence)}")
    if contains and bb:
        print(
            f"  bounding_box: x={bb['x']:.2f}  y={bb['y']:.2f}  "
            f"w={bb['w']:.2f}  h={bb['h']:.2f}  {t.elapsed()}"
        )
    else:
        print(f"  bounding_box: null  {t.elapsed()}")

    return detection, b64


def run_map_extraction(
    webp_bytes: bytes, bounding_box: dict, confidence: str, output_dir: Path, stem: str
) -> tuple[bytes, bool, dict]:
    """Stage 3: OpenCV crop + perspective + CLAHE + adaptive threshold → WebP quality=92."""
    import uuid
    import cv2
    import numpy as np

    from notes.map_extractor import _MIN_CONTRAST, _MIN_DIMENSION

    # Replicate quality gate check manually for reporting, then call the real extractor
    nparr = np.frombuffer(webp_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    img_h, img_w = img.shape[:2]

    padding = 0.02 if confidence == "high" else 0.04
    bb = bounding_box
    x = max(0.0, bb["x"] - padding)
    y = max(0.0, bb["y"] - padding)
    bw = min(1.0 - x, bb["w"] + 2 * padding)
    bh = min(1.0 - y, bb["h"] + 2 * padding)
    crop_w = int(bw * img_w)
    crop_h = int(bh * img_h)
    crop = img[int(y * img_h): int(y * img_h) + crop_h, int(x * img_w): int(x * img_w) + crop_w]
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    contrast = float(gray_crop.std())
    min_dim = min(crop.shape[:2])

    # Use a temp upload path so extract_and_correct can save its output
    import tempfile, os
    from unittest.mock import patch
    import notes.map_extractor as me

    tmp_uploads = Path(tempfile.mkdtemp())
    map_note_id = str(uuid.uuid4())

    t = _Timer()
    with patch.object(me.settings, "uploads_path", str(tmp_uploads)):
        from notes.map_extractor import extract_and_correct
        path, is_low_quality = extract_and_correct(
            webp_bytes, bounding_box, confidence, map_note_id
        )
        map_bytes = Path(path).read_bytes()

    out_path = output_dir / f"{stem}_map_extracted.webp"
    out_path.write_bytes(map_bytes)

    result = {
        "crop_px": f"{crop_w}×{crop_h}",
        "contrast": round(contrast, 1),
        "min_dim": min_dim,
        "is_low_quality": is_low_quality,
        "output_path": str(out_path),
        "output_size_kb": round(len(map_bytes) / 1024, 1),
    }

    _sep("STAGE 3 — MAP EXTRACTION (OpenCV)")
    gate_contrast = _ok(f"{contrast:.1f}") if contrast >= _MIN_CONTRAST else _err(f"{contrast:.1f} ← below {_MIN_CONTRAST}")
    gate_dim = _ok(str(min_dim)) if min_dim >= _MIN_DIMENSION else _err(f"{min_dim} ← below {_MIN_DIMENSION}")
    print(f"  Crop:      {crop_w}×{crop_h} px")
    print(f"  Contrast:  {gate_contrast}  (gate ≥{_MIN_CONTRAST})")
    print(f"  Min dim:   {gate_dim} px  (gate ≥{_MIN_DIMENSION})")
    if is_low_quality:
        print(f"  Result:    {_warn('LOW QUALITY — stored original crop (processing_notes=low_quality_scan)')}")
    else:
        print(f"  Result:    {_ok('correction applied')}  → quality=92 WebP")
    print(f"  Saved:     {out_path.name}  {_dim(f'({len(map_bytes)/1024:.0f} KB)')}  {t.elapsed()}")

    return map_bytes, is_low_quality, result


async def run_map_description(map_bytes: bytes) -> str:
    """Stage 4: Vision model generates spatial description of extracted map (prose)."""
    from llm.client import VISION_MODEL, ollama_generate
    from prompts.registry import MAP_DESCRIPTION_PROMPT

    b64 = base64.b64encode(map_bytes).decode("utf-8")
    t = _Timer()
    description = await ollama_generate(
        VISION_MODEL,
        MAP_DESCRIPTION_PROMPT,
        temperature=0.3,
        keep_alive=0,
        images=[b64],
    )

    _sep("STAGE 4 — MAP SPATIAL DESCRIPTION")
    print(f"  {description.strip()}")
    print(f"  {_dim(f'{len(description)} chars')}  {t.elapsed()}")
    return description


async def run_ocr(webp_bytes: bytes) -> tuple[str, str | None]:
    """Stage 5: OCR the notebook page + extract trip date."""
    from llm.client import VISION_MODEL, ollama_generate

    b64 = base64.b64encode(webp_bytes).decode("utf-8")
    ocr_prompt = (
        "Transcribe all text visible on this notebook page exactly as written. "
        "Then on a new line starting with 'TRIP DATE:', extract the trip date if present "
        "(format: YYYY-MM-DD or 'unknown' if not found)."
    )

    t = _Timer()
    raw = await ollama_generate(
        VISION_MODEL,
        ocr_prompt,
        temperature=0.1,
        keep_alive=0,
        images=[b64],
    )

    ocr_text = raw
    extracted_date: str | None = None
    if "TRIP DATE:" in raw:
        parts = raw.split("TRIP DATE:", 1)
        ocr_text = parts[0].strip()
        date_part = parts[1].strip().split()[0] if parts[1].strip() else ""
        if date_part and date_part != "unknown":
            extracted_date = date_part

    _sep("STAGE 5 — OCR + DATE EXTRACTION")
    print(f"  {_dim('─── Raw text ───────────────────────────────────')}")
    for line in ocr_text.splitlines():
        print(f"  {line}")
    print(f"  {_dim('─── Date ───────────────────────────────────────')}")
    if extracted_date:
        print(f"  Extracted: {_ok(extracted_date)}  ← confirm this is correct")
    else:
        print(f"  Extracted: {_warn('none found')}")
    print(f"  {_dim(f'{len(ocr_text)} chars')}  {t.elapsed()}")

    return ocr_text, extracted_date


async def run_location_extraction(ocr_text: str) -> dict:
    """Stage 6: Extract fishing location string from OCR text."""
    from llm.client import CHAT_MODEL, call_json_llm
    from prompts.registry import LOCATION_EXTRACTION_PROMPT

    prompt = LOCATION_EXTRACTION_PROMPT.format(note_text=ocr_text)
    t = _Timer()
    result = await call_json_llm(
        prompt,
        CHAT_MODEL,
        {"location_string": "", "confidence": "none"},
    )

    loc_str = result.get("location_string", "")
    conf = result.get("confidence", "none")
    conf_color = _ok if conf == "high" else (_warn if conf in ("medium", "low") else _dim)

    _sep("STAGE 6 — LOCATION EXTRACTION")
    print(f"  location_string: {_ok(repr(loc_str)) if loc_str else _warn('(empty)')}")
    print(f"  confidence:      {conf_color(conf)}  {t.elapsed()}")
    return result


async def run_spot_resolution(ocr_text: str) -> dict:
    """Stage 7: Spot entity resolution against the live spots table."""
    from db.connection import AsyncSessionLocal
    from notes.spot_resolver import resolve_spot

    t = _Timer()
    async with AsyncSessionLocal() as db:
        resolution = await resolve_spot(ocr_text, db)

    band = resolution["band"]
    candidates = resolution.get("candidates", [])
    loc_str = resolution.get("location_string", "")
    loc_conf = resolution.get("location_confidence", "none")

    band_color = _ok if band == "auto" else (_warn if band == "medium" else _dim)

    _sep("STAGE 7 — SPOT RESOLUTION")
    print(f"  location_string:    {repr(loc_str) if loc_str else _dim('(none)')}")
    print(f"  location_confidence: {loc_conf}")
    print(f"  band:               {band_color(band)}", end="")
    if band == "auto":
        print(f"  → auto-link: {_ok(candidates[0]['name'])} (score {candidates[0]['combined_score']:.2f})", end="")
    print(f"  {t.elapsed()}")

    if candidates:
        print(f"\n  {'Name':<35} {'County':<15} {'sem':>6} {'trgm':>6} {'combined':>8}")
        print(f"  {'─'*35} {'─'*15} {'─'*6} {'─'*6} {'─'*8}")
        for c in candidates:
            score = c["combined_score"]
            score_str = _ok(f"{score:.2f}") if score >= 0.85 else (_warn(f"{score:.2f}") if score >= 0.5 else f"{score:.2f}")
            print(
                f"  {c['name']:<35} {(c.get('county') or '?'):<15} "
                f"{c.get('sem_score', 0):.2f}   {c.get('trgm_score', 0):.2f}   {score_str}"
            )

    if band == "low" or not candidates:
        print(f"  {_warn('→ would trigger Create New Spot flow')}")

    return resolution


async def run_field_extraction(ocr_text: str) -> dict:
    """Stage 8: Structured field extraction from OCR text."""
    from llm.client import CHAT_MODEL, call_json_llm
    from notes.ingestion import _sanitise_fields
    from prompts.registry import FIELD_EXTRACTION_PROMPT

    _VALID_NEGATIVE_REASONS = {"conditions", "access", "fish_absence", "gear", "unknown"}

    prompt = FIELD_EXTRACTION_PROMPT.format(note_text=ocr_text)
    t = _Timer()
    raw_fields = await call_json_llm(
        prompt,
        CHAT_MODEL,
        {
            "species": [], "flies": [], "outcome": "neutral",
            "negative_reason": None, "approx_cfs": None,
            "approx_temp": None, "time_of_day": None,
        },
    )
    fields = _sanitise_fields(dict(raw_fields))

    _sep("STAGE 8 — FIELD EXTRACTION")

    outcome = fields.get("outcome", "?")
    outcome_color = _ok if outcome == "positive" else (_warn if outcome == "neutral" else _err)
    print(f"  outcome:         {outcome_color(outcome)}")

    nr = fields.get("negative_reason")
    if outcome == "negative":
        nr_ok = nr in _VALID_NEGATIVE_REASONS
        nr_str = _ok(nr) if nr_ok else _err(f"{nr!r} ← INVALID (coerced to 'unknown')")
        print(f"  negative_reason: {nr_str}")
    else:
        nr_str = _ok("null ✓") if nr is None else _err(f"should be null, got {nr!r}")
        print(f"  negative_reason: {nr_str}")

    species = fields.get("species") or []
    flies = fields.get("flies") or []
    print(f"  species:         {species if species else _dim('[]')}")
    print(f"  flies:           {flies if flies else _dim('[]')}")

    cfs = fields.get("approx_cfs")
    temp = fields.get("approx_temp")
    tod = fields.get("time_of_day")
    print(f"  approx_cfs:      {cfs if cfs is not None else _dim('null')}")
    print(f"  approx_temp:     {temp if temp is not None else _dim('null')}")
    print(f"  time_of_day:     {tod if tod is not None else _dim('null')}")
    print(f"  {t.elapsed()}")

    return fields


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def validate_image(
    image_path: Path,
    source_type: str,
    output_dir: Path,
    use_db: bool,
) -> dict:
    print(f"\n{'═' * 60}")
    print(f"{_BOLD}  {image_path.name}  ({source_type}){_RESET}")
    print(f"{'═' * 60}")

    results: dict = {"image": str(image_path), "source_type": source_type}

    # Stage 1 — always
    try:
        webp_bytes, r1 = run_upload_hardening(image_path, output_dir)
        results["upload_hardening"] = r1
    except ValueError as exc:
        print(_err(f"  FAILED: {exc}"))
        results["upload_hardening"] = {"error": str(exc)}
        return results

    ocr_text: str = ""
    extracted_date: str | None = None

    if source_type == "map":
        # Standalone map: skip detection, apply correction directly, generate spatial desc
        map_bytes, is_low_quality, r3 = run_map_extraction(
            webp_bytes,
            {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
            "high",
            output_dir,
            image_path.stem,
        )
        results["map_extraction"] = r3

        description = await run_map_description(map_bytes)
        results["map_description"] = description
        ocr_text = description  # use spatial desc for field extraction

    else:
        # Handwritten: map detection → maybe extraction → OCR
        detection, _ = await run_map_detection(webp_bytes)
        results["map_detection"] = detection

        if detection.get("contains_map") and detection.get("bounding_box"):
            map_bytes, is_low_quality, r3 = run_map_extraction(
                webp_bytes,
                detection["bounding_box"],
                detection.get("confidence", "low"),
                output_dir,
                image_path.stem,
            )
            results["map_extraction"] = r3

            description = await run_map_description(map_bytes)
            results["map_description"] = description
        else:
            results["map_extraction"] = None

        ocr_text, extracted_date = await run_ocr(webp_bytes)
        results["ocr_text"] = ocr_text
        results["extracted_date"] = extracted_date

    # Stage 6 — location extraction
    loc = await run_location_extraction(ocr_text)
    results["location_extraction"] = loc

    # Stage 7 — spot resolution (DB optional)
    if use_db:
        resolution = await run_spot_resolution(ocr_text)
        results["spot_resolution"] = resolution
    else:
        _sep("STAGE 7 — SPOT RESOLUTION")
        print(f"  {_dim('skipped (--no-db)')}")
        results["spot_resolution"] = None

    # Stage 8 — field extraction
    fields = await run_field_extraction(ocr_text)
    results["field_extraction"] = fields

    # Summary
    _sep("SUMMARY")
    r_upload = results.get("upload_hardening", {})
    r_detect = results.get("map_detection", {})
    r_extract = results.get("map_extraction", {})
    r_loc = results.get("location_extraction", {})
    r_res = results.get("spot_resolution") or {}
    r_fields = results.get("field_extraction", {})

    has_map = r_detect.get("contains_map", False) or source_type == "map"
    map_ok = (r_extract and not r_extract.get("is_low_quality")) if r_extract else False

    print(f"  Upload hardening:   {_ok('✓')}")
    if source_type == "handwritten":
        print(f"  Map detected:       {_ok('yes') if has_map else _dim('no')}")
        if has_map:
            print(f"  Map quality:        {_ok('good') if map_ok else _warn('low quality — check extracted image')}")
        print(f"  OCR text:           {_ok('✓')}  ({len(ocr_text)} chars)")
        print(f"  Date extracted:     {_ok(extracted_date) if extracted_date else _warn('not found — user must enter manually')}")
    print(f"  Location string:    {_ok(repr(r_loc.get('location_string',''))) if r_loc.get('location_string') else _warn('none — spot must be selected manually')}")
    if use_db and r_res:
        band = r_res.get("band", "?")
        band_color = _ok if band == "auto" else (_warn if band == "medium" else _err)
        print(f"  Spot band:          {band_color(band)}", end="")
        if band == "auto" and r_res.get("candidates"):
            print(f" → {r_res['candidates'][0]['name']}", end="")
        print()
    print(f"  Outcome:            {r_fields.get('outcome', '?')}")
    print(f"  Species:            {r_fields.get('species') or _dim('none')}")
    print(f"  Flies:              {r_fields.get('flies') or _dim('none')}")
    print(f"\n  Output dir: {output_dir}")

    # Save JSON results
    results_path = output_dir / f"{image_path.stem}_results.json"
    results_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"  Results JSON: {results_path.name}")

    return results


async def main_async(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{_BOLD}Fly Fish WA — Note Ingestion Validator{_RESET}")
    print(f"Output directory: {output_dir}")

    for image_path_str in args.images:
        image_path = Path(image_path_str)
        if not image_path.exists():
            print(_err(f"\nFile not found: {image_path}"))
            continue
        await validate_image(image_path, args.source_type, output_dir, not args.no_db)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate notes ingestion pipeline on test images",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("images", nargs="+", help="Image file paths to validate")
    parser.add_argument(
        "--source-type",
        choices=["handwritten", "map"],
        default="handwritten",
        help="Source type: handwritten (default) or map",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip spot resolution DB lookups (run without a live DB)",
    )
    parser.add_argument(
        "--output-dir",
        default=f"/tmp/flyfish_val_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        help="Directory to save re-encoded images and results JSON",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
