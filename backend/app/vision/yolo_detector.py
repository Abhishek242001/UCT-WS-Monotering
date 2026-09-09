"""
Real person detection using YOLOv8 (Ultralytics), verified to load and run
inference in this environment. Uses yolov8n.pt (nano) by default for speed;
swap YOLO_MODEL_PATH to yolov8s.pt or a YOLO26/RF-DETR export for higher
accuracy per the model-selection discussion in Section 11 of the project
documentation.

This module deliberately does NOT implement the event-driven trigger,
ROI-cropping, or state-machine debouncing described in Sections 3.2-3.3 --
those are orchestration concerns that belong in the stream-processing
worker (not built in this initial version, since it requires a live video
source), not in the detector wrapper itself. What's here is the piece that
IS fully real and testable without a camera: given a single frame/image,
find people and report which normalized coordinates they occupy.
"""
import os
from typing import NamedTuple

MODEL_PATH = os.environ.get("YOLO_MODEL_PATH", "yolov8n.pt")

_model = None


class PersonDetection(NamedTuple):
    confidence: float
    x1: float  # normalized 0.0-1.0, matches the ROI coordinate convention
    y1: float
    x2: float
    y2: float


def get_model():
    global _model
    if _model is None:
        from ultralytics import YOLO
        _model = YOLO(MODEL_PATH)
    return _model


PERSON_CLASS_ID = 0  # COCO class 0 == "person"


def detect_people(image_path: str, confidence_threshold: float = 0.4) -> list[PersonDetection]:
    """Runs real YOLO inference on an image file and returns normalized
    bounding boxes for every detected person above the confidence
    threshold. Coordinates are normalized to match the workstation ROI
    convention documented in Section 5.1 of the project documentation
    (x1,y1,x2,y2 in 0.0-1.0), so a detection can be directly compared
    against a saved workstation rectangle.
    """
    model = get_model()
    results = model.predict(image_path, verbose=False)
    r = results[0]
    h, w = r.orig_shape
    detections = []
    for box in r.boxes:
        cls = int(box.cls[0])
        conf = float(box.conf[0])
        if cls != PERSON_CLASS_ID or conf < confidence_threshold:
            continue
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        detections.append(PersonDetection(
            confidence=conf,
            x1=x1 / w, y1=y1 / h, x2=x2 / w, y2=y2 / h,
        ))
    return detections


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
