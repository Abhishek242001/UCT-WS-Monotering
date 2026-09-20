"""
Verifies the pose-model + tracking swap in app/vision/yolo_detector.py:
yolov8n-pose.pt replacing plain yolov8n.pt, adding persistent track IDs
(for the employee-label-follows-the-box feature) and body keypoints (for
app/logic.py's classify_activity(), which existed fully tested but had no
real keypoint source until now).

Deliberately does NOT test classify_activity() itself (already covered by
tests/design-acceptance-suite/tests/test_logic_activity_detection.py) or
any frontend/DB wiring of track_id (not yet built) -- this is scoped to
proving the detector layer itself: real track-ID persistence, real
keypoint structure, and per-worker model isolation.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def test_detect_people_still_works_unchanged_for_single_images(sample_photo_path):
    """The plain, stateless path (used by workstations.py's
    simulate_detection, which has no continuing frame sequence to track
    across) must keep working exactly as before the pose-model swap --
    same PersonDetection.x1/y1/x2/y2/confidence fields, just now also
    carrying keypoints (and no track_id, since there's nothing to
    persist across for a single image)."""
    from app.vision import yolo_detector

    people = yolo_detector.detect_people(sample_photo_path)
    assert len(people) >= 1

    p = people[0]
    assert 0.0 <= p.x1 < p.x2 <= 1.0
    assert 0.0 <= p.y1 < p.y2 <= 1.0
    assert p.track_id is None  # detect_people() never tracks
    assert p.keypoints is not None
    assert set(p.keypoints.keys()) == set(yolo_detector.COCO_KEYPOINT_NAMES)
    for name, (x, y, conf) in p.keypoints.items():
        assert 0.0 <= x <= 1.0, f"{name} x out of normalized range"
        assert 0.0 <= y <= 1.0, f"{name} y out of normalized range"
        assert 0.0 <= conf <= 1.0, f"{name} confidence out of range"


def test_track_ids_persist_across_frames_on_the_same_model_instance(sample_photo_path):
    """The actual point of switching to a pose model with tracking: a
    person detected in one frame keeps the SAME track_id in the next
    frame, when both frames go through one model instance -- this is
    what lets a recognized employee's name stay attached to their moving
    box between identification calls, instead of being re-derived (and
    potentially re-ordered/confused) from scratch every frame."""
    from app.vision import yolo_detector

    model = yolo_detector.new_model_instance()
    frame1 = yolo_detector.detect_and_track_people(model, sample_photo_path)
    frame2 = yolo_detector.detect_and_track_people(model, sample_photo_path)

    assert len(frame1) >= 1 and len(frame2) >= 1
    ids_1 = sorted(p.track_id for p in frame1)
    ids_2 = sorted(p.track_id for p in frame2)
    assert all(tid is not None for tid in ids_1), "every tracked detection should get an ID"
    assert ids_1 == ids_2, "the same people across frames should keep the same track IDs"


def test_separate_model_instances_do_not_share_track_id_state(sample_photo_path):
    """Guards the specific correctness reason new_model_instance() exists
    at all (see its docstring): each StreamWorker must get its own model
    instance, because two unrelated video sources sharing one tracker's
    internal state would either collide ID spaces or, worse, corrupt
    each other's tracking under concurrent access. A fresh instance
    should start its own ID sequence, not continue from wherever a prior
    instance's internal counter left off."""
    from app.vision import yolo_detector

    model_a = yolo_detector.new_model_instance()
    for _ in range(3):  # advance model_a's internal track state a few frames
        yolo_detector.detect_and_track_people(model_a, sample_photo_path)

    model_b = yolo_detector.new_model_instance()
    frame_b = yolo_detector.detect_and_track_people(model_b, sample_photo_path)
    # A fresh instance assigns IDs from its own sequence (starting at 1
    # for the first track), independent of how far model_a's internal
    # counter had already advanced -- proving no shared state leaked in.
    assert min(p.track_id for p in frame_b) == 1


def test_stream_worker_gets_its_own_private_model_instance(client, monkeypatch, synthetic_video):
    """StreamWorker.run() must call yolo_detector.new_model_instance()
    (a fresh instance) rather than yolo_detector.get_model() (the shared
    singleton used by the stateless single-image endpoints) -- this is a
    direct check on the wiring itself, not just its downstream effect,
    since two unrelated StreamWorker threads sharing one tracker's state
    would be a subtle, hard-to-notice bug rather than a loud failure."""
    from app.vision import yolo_detector, stream_worker

    calls = []
    original = yolo_detector.new_model_instance

    def spy():
        calls.append(True)
        return original()

    monkeypatch.setattr(yolo_detector, "new_model_instance", spy)

    worker = stream_worker.StreamWorker(source=synthetic_video, org_id=1, cam_id=999, poll_interval_seconds=0)
    worker.run()  # run synchronously (not .start()) so this test doesn't need to poll/join a thread

    assert calls, "StreamWorker.run() should request its own model instance via new_model_instance()"
    assert worker.frames_processed == 6
