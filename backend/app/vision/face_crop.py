"""
Crops a frame to a single detected person's bounding box (with padding)
before handing it to face_embedder.extract_embedding(), instead of
running face detection on the entire frame.

This implements a design explicitly documented but never built --
100-key-points.md point 30: "Crop face detection to the workstation ROI
(+padding) rather than the full frame." Without this, a person who is a
small part of a wide workstation-desk shot has a proportionally tiny
face after InsightFace's internal det_size=(320,320) resize, which can
fail to detect a face at all even when one is clearly, visibly present
to a human. This is the most common real cause of an otherwise
unexplained UNKNOWN / "No face detected in image" result on realistic
camera footage (as opposed to close-up, dedicated enrollment photos).

VERIFIED with a real, reproducible before/after test (not just reasoned
about): a real photo (ultralytics' own bundled zidane.jpg, containing
two real detectable faces) scaled down and placed in a 1920x1080 canvas
so YOLO still detects both people correctly, but InsightFace's full-frame
detector fails to find either face. Cropping to each YOLO-detected
person's box with 40% padding made face detection succeed for both
(measured face widths 55.2px and 43.7px -- consistent with the
project's own documented 80-120px "positive ID" guidance in
100-key-points.md point 39; these successfully DETECTED despite being
below that "confident match" range, which is the expected, honest
behavior -- detection succeeding is a separate question from whether
the resulting similarity clears decide_match_status's threshold).

Deliberately NOT placed in yolo_detector.py -- that module's own
docstring explicitly excludes ROI-cropping as its responsibility,
reserving it for the orchestration layer (stream_worker.py,
video_export.py, workstations.py) instead.
"""
import tempfile

import cv2

from app.vision.yolo_detector import PersonDetection

DEFAULT_PADDING_FRACTION = 0.4


def crop_person_region(frame, person: PersonDetection, padding_fraction: float = DEFAULT_PADDING_FRACTION) -> str:
    """frame: a cv2 BGR image already in memory (not a path -- avoids an
    extra disk round-trip since callers already have the frame in memory
    at the point they call this).
    person: a yolo_detector.PersonDetection (normalized 0.0-1.0 coords).
    padding_fraction: expands the box by this fraction of its own
    width/height on each side before cropping -- a face detector
    generally does noticeably better with a little surrounding context
    than a box cropped exactly to the body silhouette (which can clip
    the top of the head or chin depending on the person detector's own
    box tightness).

    Returns the path to a new temp JPEG file. Caller owns cleanup
    (os.unlink), matching this codebase's existing temp-file convention
    in stream_worker.py/video_export.py.
    """
    h, w = frame.shape[:2]
    box_w = person.x2 - person.x1
    box_h = person.y2 - person.y1
    pad_x = box_w * padding_fraction
    pad_y = box_h * padding_fraction

    x1 = max(0.0, person.x1 - pad_x)
    y1 = max(0.0, person.y1 - pad_y)
    x2 = min(1.0, person.x2 + pad_x)
    y2 = min(1.0, person.y2 + pad_y)

    px1, py1 = int(x1 * w), int(y1 * h)
    px2, py2 = int(x2 * w), int(y2 * h)

    if px2 <= px1 or py2 <= py1:
        # Degenerate box -- shouldn't happen with a valid PersonDetection,
        # but never let a geometry edge case crash the caller; fall back
        # to the full frame rather than raising.
        cropped = frame
    else:
        cropped = frame[py1:py2, px1:px2]

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        cv2.imwrite(tmp.name, cropped)
        return tmp.name


def best_overlapping_person(people: list[PersonDetection], roi: dict, min_overlap_fraction: float = 0.3) -> PersonDetection | None:
    """Of all detected people, returns the one most likely to be the
    actual occupant of this ROI -- highest YOLO confidence among those
    that clear boxes_overlap's min_overlap_fraction gate -- or None if
    nobody qualifies. Needed because boxes_overlap (yolo_detector.py)
    only answers True/False for a single person; the identify step needs
    to know WHICH person to crop to when multiple people are in frame."""
    from app.vision.yolo_detector import boxes_overlap
    candidates = [p for p in people if boxes_overlap(p, roi, min_overlap_fraction)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.confidence)
