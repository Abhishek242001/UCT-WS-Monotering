"""
Shared frame-annotation drawing: draws detected-person boxes, workstation
ROI boxes, and occupancy/identity status text directly onto a video frame
(in place, via OpenCV).

Extracted from app/vision/video_export.py's annotate_video(), which
originally had this logic inlined. It is now used by TWO callers that
need to draw identically:
  - video_export.annotate_video() -- unchanged behavior, still renders a
    downloadable annotated video.
  - stream_worker.StreamWorker -- NEW: draws the same overlay on each
    live frame before JPEG-encoding it for the live "frame" WebSocket
    push, so what you see live matches what the download feature would
    have rendered, from one place instead of two copies that could
    silently drift apart.

Colors are BGR (OpenCV's native order, not RGB).
"""
import cv2

PERSON_COLOR = (60, 200, 60)     # BGR: green
ROI_COLOR = (255, 130, 40)       # BGR: blue-orange
ACTIVE_COLOR = (0, 200, 0)       # BGR: green
VACANT_COLOR = (60, 60, 220)     # BGR: red


def draw_annotations(frame, w: int, h: int, rois: dict[str, dict],
                      last_status: dict[str, str], last_result: dict[str, tuple],
                      last_people: list) -> None:
    """Mutates `frame` in place. `w`/`h` are the frame's pixel dimensions
    (passed explicitly rather than re-read from `frame.shape`, since both
    callers already have them on hand from the capture source).

    - `rois`: workstation_name -> {"x1","y1","x2","y2"} (normalized 0-1).
    - `last_status`: workstation_name -> "ACTIVE"/"VACANT", the most
      recently computed occupancy state.
    - `last_result`: workstation_name -> (event_type, detected_employee_id,
      similarity) from the most recent identify() call, or a
      ("VACANT", None, None) default.
    - `last_people`: the most recent yolo_detector.PersonDetection list.

    Callers are expected to keep drawing with the latest CACHED values on
    every frame even when detection/identification only ran on some of
    them (both existing callers already do this) -- that's what keeps the
    overlay looking continuous rather than flickering blank between
    detection cycles.
    """
    for p in last_people:
        x1, y1 = int(p.x1 * w), int(p.y1 * h)
        x2, y2 = int(p.x2 * w), int(p.y2 * h)
        cv2.rectangle(frame, (x1, y1), (x2, y2), PERSON_COLOR, 2)
        cv2.putText(frame, f"person {p.confidence:.2f}", (x1, max(12, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, PERSON_COLOR, 1, cv2.LINE_AA)

    banner_y = 24
    for name, roi in rois.items():
        rx1, ry1 = int(roi["x1"] * w), int(roi["y1"] * h)
        rx2, ry2 = int(roi["x2"] * w), int(roi["y2"] * h)
        status = last_status.get(name, "VACANT")
        color = ACTIVE_COLOR if status == "ACTIVE" else VACANT_COLOR
        cv2.rectangle(frame, (rx1, ry1), (rx2, ry2), ROI_COLOR, 2)

        event_type, detected, similarity = last_result.get(name, ("VACANT", None, None))
        box_label = f"{name}: {status}"
        if status == "ACTIVE" and event_type != "ACTIVE":
            box_label += f" | {event_type}"
            if detected:
                box_label += f" ({detected}, sim={similarity:.2f})"
        cv2.putText(frame, box_label, (rx1, max(12, ry1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)

        cv2.putText(frame, box_label, (10, banner_y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2, cv2.LINE_AA)
        banner_y += 24
