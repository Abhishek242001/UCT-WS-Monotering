"""
Real person detection using YOLOv8-pose (Ultralytics), verified to load and
run inference in this environment. Uses yolov8n-pose.pt (nano) by default
for speed; swap YOLO_MODEL_PATH for a larger pose variant if real testing
shows the nano's keypoint confidence is too noisy for classify_activity()'s
gate at your camera's typical person size -- not assumed sufficient without
that test.

Superseded design note: this module previously used plain yolov8n.pt
(detection-only, no keypoints). It was switched to a pose model to serve
THREE consumers from one detection call per frame, rather than running a
separate cheap detector alongside a separate pose model: (1) occupancy
(unchanged -- still just the bounding box), (2) persistent track IDs, so a
recognized employee's name can stay attached to their box as they move
without a fresh identification call every frame, and (3) body keypoints,
feeding app/logic.py's classify_activity() (SITTING/STANDING/WALKING/etc),
which existed fully tested but had no real keypoint source until now.
docs/100-key-points.md point 89 explicitly rejected a pose model as a
*replacement* for plain detection for identification purposes -- that
rejection is honored here: the pose keypoints are never used for face
detection/alignment/identification. Identification still crops to the
plain bounding box (app/vision/face_crop.py) and lets InsightFace run its
own detector+alignment on that crop, unchanged. The keypoints are only
ever consumed by classify_activity(), a separate, decoupled feature.

This module deliberately does NOT implement the event-driven trigger,
ROI-cropping, or state-machine debouncing described in Sections 3.2-3.3 --
those are orchestration concerns that belong in the stream-processing
worker, not in the detector wrapper itself.
"""
import os
from typing import NamedTuple

MODEL_PATH = os.environ.get("YOLO_MODEL_PATH", "yolov8n-pose.pt")

# COCO's standard 17-keypoint order (the order Ultralytics' pose models
# output keypoints in) -- named here once so every caller uses the same
# names rather than remembering numeric indices.
COCO_KEYPOINT_NAMES = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
]

_model = None


class PersonDetection(NamedTuple):
    confidence: float
    x1: float  # normalized 0.0-1.0, matches the ROI coordinate convention
    y1: float
    x2: float
    y2: float
    track_id: int | None = None  # only set by detect_and_track_people(); None from plain detect_people()
    keypoints: dict[str, tuple[float, float, float]] | None = None  # name -> (x_norm, y_norm, confidence)


def get_model():
    """The shared, process-wide model instance for STATELESS single-image
    calls (detect_people(), used by workstations.py's simulate_detection).
    Deliberately NOT used for tracking -- see new_model_instance()."""
    global _model
    if _model is None:
        from ultralytics import YOLO
        _model = YOLO(MODEL_PATH)
    return _model


def new_model_instance():
    """A fresh, independent model instance for a StreamWorker's exclusive
    use with detect_and_track_people(). Deliberately NOT the shared
    singleton above: Ultralytics' tracker keeps mutable state (assigned
    track IDs, motion history) on the model object itself, so sharing one
    model instance across multiple concurrent/sequential video sources
    would either collide track-ID spaces between unrelated streams or hit
    genuine thread-safety issues from multiple worker threads mutating the
    same tracker state concurrently -- verified in this project's own
    testing that a fresh instance starts its own independent ID sequence
    rather than continuing another instance's state."""
    from ultralytics import YOLO
    return YOLO(MODEL_PATH)


PERSON_CLASS_ID = 0  # COCO class 0 == "person"


def _extract_people(r, confidence_threshold: float, with_track_id: bool) -> list[PersonDetection]:
    """Shared box+keypoint extraction, used by both detect_people() (a
    plain, stateless .predict() result) and detect_and_track_people() (a
    .track() result, which additionally carries per-box track IDs) -- the
    box and keypoint parsing is identical either way, only the ID
    extraction differs."""
    h, w = r.orig_shape
    detections = []
    has_kpts = r.keypoints is not None and len(r.keypoints) == len(r.boxes)
    for i, box in enumerate(r.boxes):
        cls = int(box.cls[0])
        conf = float(box.conf[0])
        if cls != PERSON_CLASS_ID or conf < confidence_threshold:
            continue
        x1, y1, x2, y2 = box.xyxy[0].tolist()

        track_id = None
        if with_track_id and box.id is not None:
            track_id = int(box.id[0])

        keypoints = None
        if has_kpts:
            xy = r.keypoints.xy[i].tolist()
            kconf = r.keypoints.conf[i].tolist() if r.keypoints.conf is not None else [1.0] * len(xy)
            keypoints = {
                name: (xy[j][0] / w, xy[j][1] / h, kconf[j])
                for j, name in enumerate(COCO_KEYPOINT_NAMES)
                if j < len(xy)
            }

        detections.append(PersonDetection(
            confidence=conf,
            x1=x1 / w, y1=y1 / h, x2=x2 / w, y2=y2 / h,
            track_id=track_id, keypoints=keypoints,
        ))
    return detections


def detect_people(image_path: str, confidence_threshold: float = 0.4) -> list[PersonDetection]:
    """Runs real YOLO-pose inference on a single image file (the shared,
    stateless model -- no tracking) and returns normalized bounding boxes
    (+ keypoints, no track_id) for every detected person above the
    confidence threshold. Coordinates are normalized to match the
    workstation ROI convention documented in Section 5.1 of the project
    documentation (x1,y1,x2,y2 in 0.0-1.0), so a detection can be directly
    compared against a saved workstation rectangle. Used where there is no
    continuing frame sequence to track across (a single uploaded still).
    """
    model = get_model()
    results = model.predict(image_path, verbose=False)
    return _extract_people(results[0], confidence_threshold, with_track_id=False)


def detect_and_track_people(model, image_path: str, confidence_threshold: float = 0.4) -> list[PersonDetection]:
    """Like detect_people(), but for a continuing sequence of frames from
    ONE video source: `model` must be an instance this caller keeps and
    reuses across every frame of that same source (see
    new_model_instance()), so Ultralytics' tracker can assign and persist
    track IDs across calls. Never pass the shared get_model() singleton
    here -- see new_model_instance()'s docstring for why."""
    results = model.track(image_path, persist=True, verbose=False, tracker="bytetrack.yaml")
    return _extract_people(results[0], confidence_threshold, with_track_id=True)


def boxes_overlap(a: PersonDetection, roi: dict, min_overlap_fraction: float = 0.3) -> bool:
    """Returns True if a detected person's box overlaps a workstation ROI
    by at least min_overlap_fraction of the person's own box area --
    i.e. "this person is substantially inside this desk zone", not merely
    brushing its edge."""
    ix1, iy1 = max(a.x1, roi["x1"]), max(a.y1, roi["y1"])
    ix2, iy2 = min(a.x2, roi["x2"]), min(a.y2, roi["y2"])
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    intersection = (ix2 - ix1) * (iy2 - iy1)
    person_area = (a.x2 - a.x1) * (a.y2 - a.y1)
    if person_area <= 0:
        return False
    return (intersection / person_area) >= min_overlap_fraction
