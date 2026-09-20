"""
Verifies item 13 -- the original flicker issue this whole recent round of
work started from: occupancy itself, not just identity matching, was
observed flipping VACANT/ACTIVE rapidly frame to frame on real footage.
Since status_changed was never itself debounced, every single flip fired
a fresh event write, identify() call, and (as of a later step) an
attendance write.

logic.apply_occupancy_hysteresis() is tested directly and exhaustively
(pure function, deterministic, fast); the last test in this file builds
a real synthetic video with a deliberate single-frame occupancy blip and
confirms, through a real StreamWorker run and real DB rows, that the
blip produces no identification event at all, while a genuinely
sustained occupancy period still does.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Pure function: logic.apply_occupancy_hysteresis
# ---------------------------------------------------------------------------

def test_first_observation_commits_immediately():
    from app import logic
    committed, pending, count = {}, {}, {}
    result = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    assert result == "ACTIVE"
    assert committed["Desk"] == "ACTIVE"


def test_single_frame_flip_does_not_commit():
    from app import logic
    committed, pending, count = {"Desk": "VACANT"}, {}, {}
    result = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    assert result == "VACANT", "one differing frame should not be enough to flip a committed status"
    assert committed["Desk"] == "VACANT"


def test_flip_that_reverts_before_threshold_does_not_commit():
    from app import logic
    committed, pending, count = {"Desk": "VACANT"}, {}, {}
    logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)  # count=1
    logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)  # count=2
    result = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "VACANT", hysteresis_frames=3)  # reverts
    assert result == "VACANT"
    assert committed["Desk"] == "VACANT"
    # And the reverted attempt must not carry over -- a fresh flip has to
    # start counting from zero again, not continue the abandoned attempt.
    result2 = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    assert result2 == "VACANT"
    assert count["Desk"] == 1


def test_sustained_flip_commits_exactly_at_threshold():
    from app import logic
    committed, pending, count = {"Desk": "VACANT"}, {}, {}
    r1 = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    r2 = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    r3 = logic.apply_occupancy_hysteresis(committed, pending, count, "Desk", "ACTIVE", hysteresis_frames=3)
    assert (r1, r2, r3) == ("VACANT", "VACANT", "ACTIVE"), "should commit on exactly the 3rd consecutive matching frame, not before"
    assert committed["Desk"] == "ACTIVE"


def test_multiple_workstations_tracked_independently():
    from app import logic
    committed, pending, count = {"DeskA": "VACANT", "DeskB": "ACTIVE"}, {}, {}
    r_a = logic.apply_occupancy_hysteresis(committed, pending, count, "DeskA", "ACTIVE", hysteresis_frames=3)
    r_b = logic.apply_occupancy_hysteresis(committed, pending, count, "DeskB", "VACANT", hysteresis_frames=3)
    assert r_a == "VACANT"  # DeskA's first differing frame -- not committed yet
    assert r_b == "ACTIVE"  # DeskB's first differing frame -- not committed yet either
    assert count["DeskA"] == 1
    assert count["DeskB"] == 1  # confirms DeskB's count wasn't accidentally shared with DeskA's


# ---------------------------------------------------------------------------
# End to end: a real single-frame occupancy blip in a real synthetic
# video produces NO identification event, while a real sustained
# occupancy period still does.
# ---------------------------------------------------------------------------

def test_single_frame_blip_produces_no_identification_event(client, admin_token, sample_photo_path):
    import cv2
    import numpy as np
    import sqlite3
    import os
    from app.vision.stream_worker import StreamWorker

    photo = cv2.imread(sample_photo_path)
    h, w = photo.shape[:2]
    blank = np.full((h, w, 3), 40, dtype="uint8")

    path = "/tmp/hysteresis_blip_test.mp4"
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (w, h))
    for _ in range(3):
        writer.write(blank)
    writer.write(photo)          # a single-frame blip -- must be filtered out
    for _ in range(3):
        writer.write(blank)
    for _ in range(5):
        writer.write(photo)      # a genuinely sustained occupancy -- must still commit and identify
    writer.release()

    h_auth = auth(admin_token)
    client.post("/employees/enroll", headers=h_auth, data={
        "org_id": 1, "employee_id": "EMP-BLIP-TEST", "name": "Blip Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h_auth, json={
        "org_id": 1, "cam_id": 700, "workstations": [{"name": "BlipDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h_auth, json={
        "org_id": 1, "cam_id": 700, "workstation_name": "BlipDesk",
        "employee_id": "EMP-BLIP-TEST", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=path, org_id=1, cam_id=700, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)

    db_url = os.environ["DATABASE_URL"]
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    events = conn.execute(
        "SELECT event_type FROM workstation_identity_events WHERE cam_id=700 ORDER BY id"
    ).fetchall()
    conn.close()
    event_types = [e[0] for e in events]

    match_count = event_types.count("MATCH")
    assert match_count == 1, (
        f"expected exactly ONE MATCH -- from the sustained occupancy period -- "
        f"with the single-frame blip filtered out entirely, got event sequence: {event_types}"
    )
