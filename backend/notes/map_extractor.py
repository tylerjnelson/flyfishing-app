"""
OpenCV document scanning pipeline for extracted map regions.

Pipeline (per §6.8 Step B):
  1. Crop image to bounding box with confidence-dependent padding
  2. Quality gate: if contrast < 30 or min_dimension < 200, store original crop
  3. Otherwise apply:
       - Perspective transform (deskew if page was photographed at angle)
       - Adaptive thresholding (handles uneven lighting, shadows)
       - Contrast normalisation via CLAHE (sharpens pencil/pen lines)
  4. Re-encode corrected crop to WebP quality=92

Stores the corrected map image to /data/uploads/{map_note_id}/original.webp.
"""

import io
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from config import settings

log = logging.getLogger(__name__)

# Quality gate thresholds from §6.8
_MIN_CONTRAST = 30.0
_MIN_DIMENSION = 200


def _order_corners(pts: np.ndarray) -> np.ndarray:
    """Order 4 corner points as [top-left, top-right, bottom-right, bottom-left]."""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left
    rect[2] = pts[np.argmax(s)]   # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def _perspective_correct(crop: np.ndarray) -> np.ndarray:
    """
    Attempt to find the largest quadrilateral contour and apply a perspective
    transform to deskew the image.  Falls back to the original crop if no
    clear 4-corner boundary is found.
    """
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 50, 150)

    contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return crop

    # Sort contours by area descending, try to find the largest quadrilateral
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    crop_area = crop.shape[0] * crop.shape[1]

    for cnt in contours[:5]:
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) > 0.4 * crop_area:
            pts = approx.reshape(4, 2).astype("float32")
            rect = _order_corners(pts)
            tl, tr, br, bl = rect
            w = max(
                np.linalg.norm(br - bl),
                np.linalg.norm(tr - tl),
            )
            h = max(
                np.linalg.norm(tr - br),
                np.linalg.norm(tl - bl),
            )
            dst = np.array(
                [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                dtype="float32",
            )
            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(crop, M, (int(w), int(h)))
            log.debug("perspective_corrected", extra={"w": int(w), "h": int(h)})
            return warped

    return crop  # no suitable quadrilateral found


def _adaptive_threshold(gray: np.ndarray) -> np.ndarray:
    """Apply adaptive thresholding to handle uneven lighting and shadows."""
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )


def _clahe_normalise(gray: np.ndarray) -> np.ndarray:
    """CLAHE contrast normalisation — sharpens pencil/pen lines on notebook paper."""
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(gray)


def _to_webp(img_bgr: np.ndarray, quality: int = 92) -> bytes:
    """Encode a BGR OpenCV image to WebP bytes."""
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    pil = Image.fromarray(rgb)
    buf = io.BytesIO()
    pil.save(buf, format="WEBP", quality=quality)
    return buf.getvalue()


def extract_and_correct(
    source_bytes: bytes,
    bounding_box: dict,
    confidence: str,
    map_note_id: str,
) -> tuple[str, bool]:
    """
    Crop the detected map region, apply document scanning correction, store result.

    Returns (stored_path, is_low_quality).

    is_low_quality=True means the quality gate failed and the uncorrected crop
    was stored instead — the caller should set processing_notes='low_quality_scan'.
    """
    nparr = np.frombuffer(source_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image for map extraction")

    img_h, img_w = img.shape[:2]

    # Apply padding based on detection confidence (§18.3 usage note)
    padding = 0.02 if confidence == "high" else 0.04
    bb = bounding_box
    x = max(0.0, bb["x"] - padding)
    y = max(0.0, bb["y"] - padding)
    bw = min(1.0 - x, bb["w"] + 2 * padding)
    bh = min(1.0 - y, bb["h"] + 2 * padding)

    px = int(x * img_w)
    py = int(y * img_h)
    pw = int(bw * img_w)
    ph = int(bh * img_h)

    crop = img[py : py + ph, px : px + pw]

    # Quality gate (§6.8 Step B exit)
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    contrast = float(gray_crop.std())
    min_dim = min(crop.shape[:2])
    is_low_quality = contrast < _MIN_CONTRAST or min_dim < _MIN_DIMENSION

    if is_low_quality:
        log.info(
            "map_low_quality",
            extra={
                "map_note_id": map_note_id,
                "contrast": round(contrast, 1),
                "min_dim": min_dim,
            },
        )
        result_bgr = crop
    else:
        # Step 1: Perspective correction (deskew)
        corrected = _perspective_correct(crop)
        gray = cv2.cvtColor(corrected, cv2.COLOR_BGR2GRAY)

        # Step 2: CLAHE contrast normalisation
        normalised = _clahe_normalise(gray)

        # Step 3: Adaptive thresholding — run on CLAHE output for best line clarity
        thresh = _adaptive_threshold(normalised)

        # Blend: use CLAHE normalised for colour-rich maps, threshold for pencil sketches.
        # Heuristic: if the normalised image has low variance (mostly pencil lines), use
        # threshold; otherwise preserve the greyscale texture.
        if normalised.std() < 50:
            final_gray = thresh
        else:
            final_gray = normalised

        result_bgr = cv2.cvtColor(final_gray, cv2.COLOR_GRAY2BGR)

    webp_bytes = _to_webp(result_bgr, quality=92)

    dir_path = Path(settings.uploads_path) / map_note_id
    dir_path.mkdir(parents=True, exist_ok=True)
    dest = dir_path / "original.webp"
    dest.write_bytes(webp_bytes)

    log.info(
        "map_extracted",
        extra={
            "map_note_id": map_note_id,
            "bytes": len(webp_bytes),
            "is_low_quality": is_low_quality,
        },
    )
    return str(dest), is_low_quality


def correct_standalone_map(source_bytes: bytes, map_note_id: str) -> tuple[str, bool]:
    """
    Apply document scanning correction to a standalone map upload (no bounding box).
    The entire image is treated as the map region.

    Returns (stored_path, is_low_quality).
    """
    full_bb = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    return extract_and_correct(source_bytes, full_bb, "high", map_note_id)
