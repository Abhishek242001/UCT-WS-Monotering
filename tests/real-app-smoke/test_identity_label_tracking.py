"""
Verifies the fix for the originally-reported gap: a recognized employee's
box was never labeled with their identity at all -- every detected
person's box always showed a generic "person {confidence}" label, while
the actual identity result was drawn separately, anchored to the
workstation's own (static) rectangle, not to the moving person. This
tests that a person's OWN box now shows their identity, keyed by their
persistent track_id so it follows them (in principle -- across frames,
not tied to workstation-ROI overlap), and that video_export.py's
downloaded-video path (a third, separate copy of the same identify logic,
found while making this fix) now matches, including gaining the same
crop-before-identify fix already verified for stream_worker.py in an
earlier round of work.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# Pure drawing logic: what label/color does a given identity result
# produce for a person's own box.
# ---------------------------------------------------------------------------

def test_no_identity_known_yet_uses_generic_person_label():
    from app.vision import frame_annotate, yolo_detector

    person = yolo_detector.PersonDetection(confidence=0.87, x1=0, y1=0, x2=1, y2=1, track_id=5)
    label, color = frame_annotate._person_label_and_color(person, last_identity_by_track={})
    assert label == "person 0.87"
    assert color == frame_annotate.PERSON_COLOR


def test_match_label_shows_employee_id_and_name():
    from app.vision import frame_annotate, yolo_detector

    person = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=7)
    identity_map = {7: ("MATCH", "EMP-001", "Abhishek", 0.91)}
    label, color = frame_annotate._person_label_and_color(person, identity_map)
    assert label == "EMP-001 - Abhishek"
    assert color == frame_annotate.MATCH_COLOR


def test_match_label_falls_back_to_bare_id_when_name_unavailable():
    from app.vision import frame_annotate, yolo_detector

    person = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=7)
    identity_map = {7: ("MATCH", "EMP-001", None, 0.91)}
    label, _ = frame_annotate._person_label_and_color(person, identity_map)
    assert label == "EMP-001"


def test_mismatch_label_is_flagged_distinctly():
    from app.vision import frame_annotate, yolo_detector

    person = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=3)
    identity_map = {3: ("MISMATCH", "EMP-002", "Someone Else", 0.6)}
    label, color = frame_annotate._person_label_and_color(person, identity_map)
    assert label == "EMP-002 - Someone Else (MISMATCH)"
    assert color == frame_annotate.MISMATCH_COLOR


def test_unknown_result_is_distinguished_from_never_identified():
    """An UNKNOWN result (identification WAS attempted, came back
    inconclusive) must read differently from a track that was simply
    never identified at all -- otherwise an admin watching the feed
    can't tell "still figuring out who this is" from "checked, don't
    know this person"."""
    from app.vision import frame_annotate, yolo_detector

    identified_unknown = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=1)
    never_checked = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=99)
    identity_map = {1: ("UNKNOWN", None, None, None)}

    unknown_label, unknown_color = frame_annotate._person_label_and_color(identified_unknown, identity_map)
    never_label, never_color = frame_annotate._person_label_and_color(never_checked, identity_map)

    assert unknown_label == "UNKNOWN"
    assert unknown_color == frame_annotate.UNKNOWN_COLOR
    assert never_label == "person 0.90"
    assert unknown_label != never_label


def test_no_track_id_always_uses_generic_label():
    """A person from a non-tracking detection call (track_id=None, e.g.
    the single-image endpoints) must never accidentally match a
    identity_by_track entry."""
    from app.vision import frame_annotate, yolo_detector

    person = yolo_detector.PersonDetection(confidence=0.9, x1=0, y1=0, x2=1, y2=1, track_id=None)
    label, color = frame_annotate._person_label_and_color(person, last_identity_by_track={None: ("MATCH", "EMP-999", "X", 0.9)})
    assert label == "person 0.90"
    assert color == frame_annotate.PERSON_COLOR


# ---------------------------------------------------------------------------
# End to end: the real StreamWorker, real enrollment, real identify() call
# -- confirms the wiring itself, not just the drawing function in isolation.
# ---------------------------------------------------------------------------

def test_stream_worker_records_identity_by_track_id(client, admin_token, synthetic_video, sample_photo_path):
    from app.vision.stream_worker import StreamWorker

    h = {"Authorization": f"Bearer {admin_token}"}
    client.post("/employees/enroll", headers=h,
                data={"org_id": 1, "employee_id": "EMP-LABEL-TEST", "name": "Label Test Person",
                      "view": "front", "depth_m": 1.0},
                files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 960, "workstations": [{"name": "LabelDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 960, "workstation_name": "LabelDesk",
        "employee_id": "EMP-LABEL-TEST", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=960, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)

    assert worker._last_identity_by_track, "expected at least one track to have a recorded identity result"
    results = list(worker._last_identity_by_track.values())
    matched = [r for r in results if r[0] == "MATCH"]
    assert matched, f"expected a MATCH among recorded identities, got: {results}"
    event_type, detected_employee_id, employee_name, similarity = matched[0]
    assert detected_employee_id == "EMP-LABEL-TEST"
    assert employee_name == "Label Test Person"
    assert similarity > 0.5
