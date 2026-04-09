"""
Dry-run map image processing pipeline — no database writes.

Runs the image through validate_and_encode() and correct_standalone_map()
and saves the corrected image to flyfish/test-notes/output/ for inspection.

Usage:
  sudo /opt/flyfish/venv/bin/python scripts/test_map_pipeline_only.py /path/to/image.jpg
"""

import io
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

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
sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image
from notes.upload_handler import validate_and_encode
from notes.map_extractor import correct_standalone_map
from notes import map_extractor

if len(sys.argv) < 2:
    print("Usage: test_map_pipeline_only.py <image_path>")
    sys.exit(1)

image_path = sys.argv[1]
print(f"Input:  {image_path}")
print(f"Size:   {Path(image_path).stat().st_size:,} bytes")

# Step 1: validate and re-encode to WebP
print("\n[1] validate_and_encode()...")
source_bytes = Path(image_path).read_bytes()
webp_bytes = validate_and_encode(source_bytes)
pil = Image.open(io.BytesIO(webp_bytes))
print(f"    → {pil.format} {pil.size[0]}×{pil.size[1]}px  {len(webp_bytes):,} bytes")

# Step 2: OpenCV correction in a temp directory (no /data/uploads write)
print("\n[2] correct_standalone_map()...")
with tempfile.TemporaryDirectory() as tmp:
    original_uploads = map_extractor.settings.uploads_path
    map_extractor.settings.uploads_path = tmp

    fake_id = str(uuid.uuid4())
    corrected_path, is_low_quality = correct_standalone_map(webp_bytes, fake_id)

    map_extractor.settings.uploads_path = original_uploads

    result_bytes = Path(corrected_path).read_bytes()
    pil2 = Image.open(io.BytesIO(result_bytes))
    print(f"    → {pil2.format} {pil2.size[0]}×{pil2.size[1]}px  {len(result_bytes):,} bytes")
    print(f"    → is_low_quality: {is_low_quality}")

    out_dir = Path("/home/ubuntu/flyfish/test-notes/output")
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / (Path(image_path).stem + "_corrected.webp")
    shutil.copy(corrected_path, output)

print(f"\nOutput: {output}")
print("No database writes performed.")
