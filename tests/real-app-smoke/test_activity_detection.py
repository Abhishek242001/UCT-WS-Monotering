"""
Verifies the activity-classification wiring: app/logic.py's
classify_activity() existed fully unit-tested (see
tests/design-acceptance-suite/tests/test_logic_activity_detection.py) but
had no real keypoint source anywhere in the codebase. This tests the two
new pieces that connect it to the real pipeline: the adapter functions
(app/logic.py combine_side_pair / activity_inputs_from_coco_keypoints)
and stream_worker.py's per-frame call + debounced "activity" WebSocket
publish.

Per docs/100-key-points.md points 91-93: activity classification is
intentionally decoupled from occupancy/identity, and UNKNOWN is expected
to be the single MOST COMMON class in practice (most of the lower body is
desk-occluded, or in this test's sample photo, simply not visible/framed).
This suite treats that as ground truth to test against, not a surprising
failure -- see test_classify_activity_on_real_photo_is_unknown below,
which is grounded in real, directly-observed keypoint confidences on the
actual bundled test photo, not assumed.
"""
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def upload(client, admin_token, video_path, org_id=1, filename="demo.mp4"):
    with open(video_path, "rb") as f:
        return client.post("/videos/upload", headers=auth(admin_token),
                            data={"org_id": org_id}, files={"file": (filename, f, "video/mp4")})


# ---------------------------------------------------------------------------
# Pure logic: the COCO-keypoints adapter, with synthetic (fully-controlled)
# keypoint dicts -- same testing philosophy already used by
# test_logic_activity_detection.py for classify_activity() itself.
# ---------------------------------------------------------------------------

def test_combine_side_pair_picks_higher_confidence_side():
    from app import logic
    keypoints = {"left_hip": (0.1, 0.2, 0.9), "right_hip": (0.3, 0.4, 0.2)}
    x, y, conf = logic.combine_side_pair(keypoints, "left_hip", "right_hip")
    assert conf == 0.9
    assert (x, y) == (0.1, 0.2)  # the higher-confidence side's own position, not an average


def test_combine_side_pair_handles_one_side_missing_entirely():
    from app import logic
    keypoints = {"right_ankle": (0.5, 0.6, 0.7)}
    x, y, conf = logic.combine_side_pair(keypoints, "left_ankle", "right_ankle")
    assert conf == 0.7
    assert (x, y) == (0.5, 0.6)


def test_combine_side_pair_both_absent_returns_zero_confidence():
    from app import logic
    x, y, conf = logic.combine_side_pair({}, "left_knee", "right_knee")
    assert conf == 0.0


def test_activity_inputs_vertical_torso_gives_near_zero_angle():
    """Shoulder directly above hip (same x, hip below in image
    coordinates) should recover torso_angle_deg == 0, matching
    classify_activity()'s documented convention."""
    from app import logic
    keypoints = {
        "left_shoulder": (0.5, 0.3, 0.9), "right_shoulder": (0.5, 0.3, 0.9),
        "left_hip": (0.5, 0.6, 0.9), "right_hip": (0.5, 0.6, 0.9),
        "left_knee": (0.5, 0.8, 0.9), "right_knee": (0.5, 0.8, 0.9),
        "left_ankle": (0.5, 0.95, 0.9), "right_ankle": (0.5, 0.95, 0.9),
    }
    keypoint_confidence, torso_angle_deg = logic.activity_inputs_from_coco_keypoints(keypoints)
    assert keypoint_confidence == {"shoulder": 0.9, "hip": 0.9, "knee": 0.9, "ankle": 0.9}
    assert torso_angle_deg == pytest.approx(0.0, abs=0.01)


def test_activity_inputs_leaning_torso_gives_signed_nonzero_angle():
    """Hip offset to one side of the shoulder (a leaning torso) should
    recover a nonzero angle, with sign depending on which direction --
    matching classify_activity()'s symmetric LEANING test cases for both
    positive and negative angles."""
    from app import logic
    keypoints_right = {
        "left_shoulder": (0.5, 0.3, 0.9), "right_shoulder": (0.5, 0.3, 0.9),
        "left_hip": (0.7, 0.6, 0.9), "right_hip": (0.7, 0.6, 0.9),
        "left_knee": (0.5, 0.8, 0.9), "right_knee": (0.5, 0.8, 0.9),
        "left_ankle": (0.5, 0.95, 0.9), "right_ankle": (0.5, 0.95, 0.9),
    }
    _, angle_right = logic.activity_inputs_from_coco_keypoints(keypoints_right)
    assert angle_right > 30  # per classify_activity()'s own LEANING threshold

    keypoints_left = dict(keypoints_right)
    keypoints_left["left_hip"] = keypoints_left["right_hip"] = (0.3, 0.6, 0.9)
    _, angle_left = logic.activity_inputs_from_coco_keypoints(keypoints_left)
    assert angle_left < -30
    assert angle_left == pytest.approx(-angle_right, abs=0.01)  # symmetric, as classify_activity() expects


def test_activity_inputs_missing_torso_keypoints_returns_zero_angle_not_a_crash():
    from app import logic
    keypoint_confidence, torso_angle_deg = logic.activity_inputs_from_coco_keypoints({})
    assert keypoint_confidence == {"shoulder": 0.0, "hip": 0.0, "knee": 0.0, "ankle": 0.0}
    assert torso_angle_deg == 0.0


# ---------------------------------------------------------------------------
# Grounded in a real photo's real, directly-observed keypoints (not a
# synthetic mock) -- confirms the documented expectation (point 93) holds
# through the full real pipeline, not just in isolated unit tests.
# ---------------------------------------------------------------------------

def test_classify_activity_on_real_photo_is_unknown(sample_photo_path):
    """Directly observed on this exact bundled photo before writing this
    test: knee/ankle keypoint confidence is ~0.00-0.04 for both people
    (a torso-up sports photo; legs are angled away/out of clear view).
    That's below classify_activity()'s MIN_KEYPOINT_CONFIDENCE=0.5 gate,
    so UNKNOWN is the correct, honest answer here -- not a bug, and
    exactly the kind of real-world case point 93 anticipates."""
    from app import logic
    from app.vision import yolo_detector

    people = yolo_detector.detect_people(sample_photo_path)
    assert len(people) >= 1

    for person in people:
        keypoint_confidence, torso_angle_deg = logic.activity_inputs_from_coco_keypoints(person.keypoints)
        activity = logic.classify_activity(keypoint_confidence, torso_angle_deg, is_moving=False)
        assert activity == logic.Activity.UNKNOWN


# ---------------------------------------------------------------------------
# Full pipeline, through the real StreamWorker and live WebSocket --
# mirrors test_video_analysis.py's own pattern for the "frame" message
# type, applied to the new "activity" message type.
# ---------------------------------------------------------------------------

def test_stream_worker_publishes_activity_events_debounced_per_track(client, admin_token, synthetic_video):
    """Runs the real synthetic_video (3 blank frames, then 3 frames of
    the real sample photo) through the actual /videos/analyze pipeline
    and WebSocket, and confirms:
      1. At least one "activity" message is published, with the exact
         UNKNOWN result test_classify_activity_on_real_photo_is_unknown
         establishes is correct for this photo's real keypoints.
      2. It's published ONLY ONCE per track_id despite the photo
         repeating for 3 frames -- the debounce-on-change logic in
         _classify_and_publish_activity, mirroring the same debounce
         pattern already verified for VACANT events
         (test_stream_worker_does_not_identify_on_every_occupied_frame).
    """
    video_id = upload(client, admin_token, synthetic_video).json()["video_id"]
    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"max_frames": 6, "poll_interval_seconds": 0})
    stream_id = analyze.json()["stream_id"]

    with client.websocket_connect(f"/ws/streams/{stream_id}?token={admin_token}") as ws:
        messages = []
        while True:
            msg = ws.receive_json()
            messages.append(msg)
            if msg["type"] == "completed":
                break

    activity_msgs = [m for m in messages if m["type"] == "activity"]
    assert activity_msgs, "expected at least one activity message for the real people in the synthetic video"

    for m in activity_msgs:
        assert m["activity"] == "UNKNOWN"  # grounded in this photo's real keypoints, see test above
        assert isinstance(m["track_id"], int)

    # Debounce check: each distinct track_id should appear at most once,
    # since its classified activity never changes (stays UNKNOWN) across
    # the 3 repeated frames of the same photo.
    track_ids_seen = [m["track_id"] for m in activity_msgs]
    assert len(track_ids_seen) == len(set(track_ids_seen)), \
        "each track_id's activity should publish once per genuine change, not once per frame"
