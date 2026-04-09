"""
OpenCV document scanning pipeline for standalone map uploads.

Pipeline:
  1. Quality gate: if contrast < 30 or min_dimension < 200, store original image
  2. Otherwise apply:
       - Perspective transform (deskew if photo taken at angle)
       - CLAHE contrast normalisation (sharpens pencil/pen lines)
       - Adaptive thresholding (handles uneven lighting, shadows)
  3. Re-encode to WebP quality=92

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

    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    crop_area = crop.shape[0] * crop.shape[1]

    for cnt in contours[:5]:
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and cv2.contourArea(approx) > 0.4 * crop_area:
            pts = approx.reshape(4, 2).astype("float32")
            rect = _order_corners(pts)
            tl, tr, br, bl = rect
            w = max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl))
            h = max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl))
            dst = np.array(
                [[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                dtype="float32",
            )
            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(crop, M, (int(w), int(h)))
            log.debug("perspective_corrected", extra={"w": int(w), "h": int(h)})
            return warped

    return crop


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


def correct_standalone_map(source_bytes: bytes, map_note_id: str) -> tuple[str, bool]:
    """
    Apply document scanning correction to a standalone map upload.
    The entire image is treated as the map region.

    Returns (stored_path, is_low_quality).

    is_low_quality=True means the quality gate failed and the uncorrected image
    was stored instead — the caller should set processing_notes='low_quality_scan'.
    """
    nparr = np.frombuffer(source_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image for map correction")

    # Quality gate
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    contrast = float(gray.std())
    min_dim = min(img.shape[:2])
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
        result_bgr = img
    else:
        corrected = _perspective_correct(img)
        gray_corrected = cv2.cvtColor(corrected, cv2.COLOR_BGR2GRAY)
        normalised = _clahe_normalise(gray_corrected)
        thresh = _adaptive_threshold(normalised)
        # Use threshold for pencil-dominated images, CLAHE for richer content
        final_gray = thresh if normalised.std() < 50 else normalised
        result_bgr = cv2.cvtColor(final_gray, cv2.COLOR_GRAY2BGR)

    webp_bytes = _to_webp(result_bgr, quality=92)

    dir_path = Path(settings.uploads_path) / map_note_id
    dir_path.mkdir(parents=True, exist_ok=True)
    dest = dir_path / "original.webp"
    dest.write_bytes(webp_bytes)

    log.info(
        "map_corrected",
        extra={
            "map_note_id": map_note_id,
            "bytes": len(webp_bytes),
            "is_low_quality": is_low_quality,
        },
    )
    return str(dest), is_low_quality
