"""
Regression test for the crop-to-person-box fix (app/vision/face_crop.py).

Implements the design documented but never built in 100-key-points.md
point 30: "Crop face detection to the workstation ROI (+padding) rather
than the full frame." Without it, InsightFace's own face detector runs
on the WHOLE frame at det_size=(320,320) -- a person who is a small part
of a wide workstation-desk shot has a proportionally tiny face after that
internal resize, which can fail detection entirely even though YOLO
correctly finds the person and the face is clearly visible to a human.

This test builds a real, reproducible version of exactly that scenario
(not a mock): a real photo with real faces (ultralytics' own bundled
zidane.jpg), scaled down and placed in a wide canvas so YOLO still
detects the person but a naive full-frame face detector fails -- then
verifies the real API (POST /workstations/simulate_detection) now
correctly identifies the person via the crop-first path.
"""
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture()
def wide_frame_with_small_face(sample_photo_path, tmp_path):
    """Places a shrunk copy of the real sample photo into a much larger
    blank canvas, at a scale empirically confirmed to keep YOLO's person
    detection working while making InsightFace's OWN full-frame face
    detector fail -- i.e. the exact failure mode this fix addresses, not
    a face that's simply undetectable under any circumstances.

    Also returns the mapping needed to crop the SAME individual's face
    from the original full-resolution photo, for use as a real
    enrollment photo -- YOLO's detection order isn't guaranteed to
    correspond to the same physical person across different images, so
    the two crops must be geometrically derived from one another, not
    assumed to share an index.
    """
    from app.vision import yolo_detector, face_crop

    orig = cv2.imread(sample_photo_path)
    oh, ow = orig.shape[:2]
    scale = 0.4  # confirmed: YOLO still detects people; full-frame face detection fails
    sw, sh = int(ow * scale), int(oh * scale)
    small = cv2.resize(orig, (sw, sh))

    canvas_w, canvas_h = 1920, 1080
    canvas = np.full((canvas_h, canvas_w, 3), 60, dtype="uint8")
    off_x, off_y = (canvas_w - sw) // 2, (canvas_h - sh) // 2
    canvas[off_y:off_y + sh, off_x:off_x + sw] = small

    wide_path = str(tmp_path / "wide_frame.jpg")
    cv2.imwrite(wide_path, canvas)

    people = yolo_detector.detect_people(wide_path)
    assert len(people) >= 1, "fixture invariant broken: YOLO should still detect people in the wide frame"
    roi = {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}
    picked = face_crop.best_overlapping_person(people, roi)
    assert picked is not None

    # Map the picked person's box back to the ORIGINAL full-res photo's
    # pixel coordinates, so the enrollment crop is guaranteed to be the
    # same physical individual as the one simulate_detection will find.
    px1, py1 = picked.x1 * canvas_w, picked.y1 * canvas_h
    px2, py2 = picked.x2 * canvas_w, picked.y2 * canvas_h
    ox1, oy1 = (px1 - off_x) / scale, (py1 - off_y) / scale
    ox2, oy2 = (px2 - off_x) / scale, (py2 - off_y) / scale
    enroll_crop = orig[max(0, int(oy1)):int(oy2), max(0, int(ox1)):int(ox2)]
    enroll_path = str(tmp_path / "enroll_photo.jpg")
    cv2.imwrite(enroll_path, enroll_crop)

    return {"wide_frame_path": wide_path, "enroll_photo_path": enroll_path}


def test_full_frame_face_detection_fails_on_small_face(wide_frame_with_small_face):
    """Establishes the failure this fix addresses is real, on the actual
    face_embedder module -- not a strawman. If this assertion ever starts
    failing, det_size or the detector pack changed enough that the crop
    fix's justification should be re-evaluated, not silently left stale."""
    from app.vision import face_embedder
    with pytest.raises(ValueError, match="No face detected"):
        face_embedder.extract_embedding(wide_frame_with_small_face["wide_frame_path"])


def test_simulate_detection_matches_person_via_crop(client, admin_token, wide_frame_with_small_face):
    """The actual regression test: through the real API, an employee
    enrolled from a close-up photo IS correctly matched against a wide
    frame where their face is small -- proving the crop-to-person-box
    path in workstations.py's simulate_detection works end to end, not
    just in isolation."""
    h = {"Authorization": f"Bearer {admin_token}"}

    with open(wide_frame_with_small_face["enroll_photo_path"], "rb") as f:
        resp = client.post("/employees/enroll", headers=h, data={
            "org_id": 1, "employee_id": "EMP-CROP-TEST", "name": "Crop Regression Test",
            "view": "front", "depth_m": 1.0,
        }, files={"photo": ("front.jpg", f, "image/jpeg")})
    assert resp.status_code == 201, resp.text

    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 950, "workstations": [{"name": "CropRegressionDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 950, "workstation_name": "CropRegressionDesk",
        "employee_id": "EMP-CROP-TEST", "effective_from": "2026-01-01",
    })

    with open(wide_frame_with_small_face["wide_frame_path"], "rb") as f:
        resp = client.post("/workstations/simulate_detection", headers=h, data={
            "org_id": 1, "cam_id": 950, "workstation_name": "CropRegressionDesk",
        }, files={"frame": ("wide.jpg", f, "image/jpeg")})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["occupied"] is True
    # Without the crop fix this would be event_type="UNKNOWN",
    # detected_employee_id=None, similarity=None -- extract_embedding
    # raising ValueError on the full frame (see the test above).
    assert body["similarity"] is not None, "face detection failed -- crop-before-detect regression"
    assert body["event_type"] == "MATCH"
    assert body["detected_employee_id"] == "EMP-CROP-TEST"
    assert body["similarity"] > 0.8  # same person, should be a strong match, not a borderline one


@pytest.fixture()
def synthetic_video_with_small_face(wide_frame_with_small_face, tmp_path):
    """Same real small-face-in-a-wide-frame scenario as
    wide_frame_with_small_face, but as a short video instead of a single
    image, so it can drive an actual StreamWorker -- the live/video-
    analysis pipeline (stream_worker.py) -- rather than only the
    single-image simulate_detection endpoint the two tests above already
    cover. Blank frames first (VACANT), then the wide frame repeated
    (ACTIVE), matching the shape of the synthetic_video fixture used by
    test_stream_worker.py."""
    import cv2
    import numpy as np

    wide = cv2.imread(wide_frame_with_small_face["wide_frame_path"])
    h, w = wide.shape[:2]
    blank = np.full((h, w, 3), 40, dtype="uint8")

    path = str(tmp_path / "synthetic_small_face.mp4")
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (w, h))
    for _ in range(3):
        writer.write(blank)
    for _ in range(3):
        writer.write(wide)
    writer.release()
    return {"video_path": path, "enroll_photo_path": wide_frame_with_small_face["enroll_photo_path"]}


def test_stream_worker_matches_person_via_crop(client, admin_token, synthetic_video_with_small_face):
    """The live-pipeline counterpart to test_simulate_detection_matches_
    person_via_crop above: proves stream_worker.py's _identify() -- used
    by both the Live Stream and Video Analysis tabs, NOT just the
    single-image "try it" endpoint -- now also crops to the occupying
    person before extracting a face embedding, instead of passing the
    full frame.

    Before this fix, stream_worker._identify() called
    face_embedder.extract_embedding() on the full, uncropped frame. Given
    this exact scenario (a real, enrolled person whose face is a small
    fraction of a wide frame), that full-frame call raises ValueError
    (proven directly by test_full_frame_face_detection_fails_on_small_face
    above, against the same frame), which stream_worker.py's old code
    caught and turned into a silent event_type="UNKNOWN" -- exactly the
    "low recognition performance" / "not bounded with person bounding
    box" symptom reported against the real live system. This test proves
    that no longer happens: the same frame, run through the real
    StreamWorker end to end, now produces a correct MATCH.
    """
    from app.vision.stream_worker import StreamWorker

    h = {"Authorization": f"Bearer {admin_token}"}
    with open(synthetic_video_with_small_face["enroll_photo_path"], "rb") as f:
        resp = client.post("/employees/enroll", headers=h, data={
            "org_id": 1, "employee_id": "EMP-STREAM-CROP", "name": "Stream Crop Regression Test",
            "view": "front", "depth_m": 1.0,
        }, files={"photo": ("front.jpg", f, "image/jpeg")})
    assert resp.status_code == 201, resp.text

    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 951, "workstations": [{"name": "StreamCropDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 951, "workstation_name": "StreamCropDesk",
        "employee_id": "EMP-STREAM-CROP", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video_with_small_face["video_path"], org_id=1, cam_id=951, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)
    assert worker.frames_processed == 6

    status = client.get("/workstations/identity_status", params={"org_id": 1, "cam_id": 951}, headers=h)
    ws = status.json()["workstations"][0]
    # Without the crop fix in stream_worker.py, this would be
    # match_status="UNKNOWN", detected_employee_id=None, similarity=None
    # -- extract_embedding raising ValueError on the full frame.
    assert ws["similarity"] is not None, "face detection failed in the live pipeline -- crop-before-detect regression"
    assert ws["match_status"] == "MATCH"
    assert ws["detected_employee_id"] == "EMP-STREAM-CROP"
    assert ws["similarity"] > 0.8
