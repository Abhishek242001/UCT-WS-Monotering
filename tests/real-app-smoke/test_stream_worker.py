"""
Tests the real stream-processing worker (app/vision/stream_worker.py)
against a synthetic but genuinely real video: blank frames (no person)
followed by frames containing real, YOLO-detectable people, built fresh
in a fixture rather than checked in as a binary asset.

This proves the event-driven detection loop actually works against a
multi-frame video source -- occupancy transitions, the VACANT->ACTIVE
identification trigger, and real database persistence -- not just against
a single uploaded still image (which /workstations/simulate_detection
already covers in test_smoke.py).
"""
import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def test_stream_worker_processes_all_frames(client, admin_token, synthetic_video):
    from app.vision.stream_worker import StreamWorker
    from app.database import SessionLocal
    from app.models import WorkstationIdentityEvent

    client.post("/workstations/save", headers={"Authorization": f"Bearer {admin_token}"}, json={
        "org_id": 1, "cam_id": 700, "workstations": [{"name": "Stream-Desk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=700, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)

    assert worker.source_opened is True
    assert worker.frames_processed == 6


def test_stream_worker_transitions_vacant_to_active_and_identifies(client, admin_token, synthetic_video, sample_photo_path):
    from app.vision.stream_worker import StreamWorker
    from app.database import SessionLocal
    from app.models import WorkstationIdentityEvent
    from app.vision import face_embedder

    h = {"Authorization": f"Bearer {admin_token}"}
    client.post("/employees/enroll", headers=h,
                data={"org_id": 1, "employee_id": "EMP-STREAM", "name": "Stream Test", "view": "front", "depth_m": 1.0},
                files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 701, "workstations": [{"name": "Stream-Desk-2", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 701, "workstation_name": "Stream-Desk-2",
        "employee_id": "EMP-STREAM", "effective_from": "2026-09-06",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=701, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)
    assert worker.frames_processed == 6

    status = client.get("/workstations/identity_status", params={"org_id": 1, "cam_id": 701}, headers=h)
    ws = status.json()["workstations"][0]
    # The final recorded state should reflect the person who appeared in
    # the second half of the video, correctly matched against enrollment.
    assert ws["match_status"] == "MATCH"
    assert ws["detected_employee_id"] == "EMP-STREAM"
    assert ws["similarity"] > 0.5


def test_stream_worker_does_not_identify_on_every_occupied_frame(client, admin_token, synthetic_video):
    """Verifies the event-driven design goal directly: with 3 vacant frames
    followed by 3 occupied frames and a heartbeat far longer than the test
    run, only ONE identification-triggering event should be written for
    the occupied period -- not three."""
    from app.vision.stream_worker import StreamWorker
    from app.database import SessionLocal
    from app.models import WorkstationIdentityEvent

    h = {"Authorization": f"Bearer {admin_token}"}
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 702, "workstations": [{"name": "Stream-Desk-3", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=702, poll_interval_seconds=0)
    worker.start()
    worker.join(timeout=60)

    # Query the real database directly to count exactly how many event rows
    # were written for the occupied period.
    sys.path.insert(0, str(BACKEND_DIR))
    db_url = os.environ["DATABASE_URL"]
    import sqlite3
    conn = sqlite3.connect(db_url.replace("sqlite:///", ""))
    rows = conn.execute(
        "SELECT event_type FROM workstation_identity_events WHERE cam_id=702 ORDER BY id"
    ).fetchall()
    conn.close()

    event_types = [r[0] for r in rows]
    assert event_types.count("VACANT") == 3
    non_vacant = [e for e in event_types if e != "VACANT"]
    assert len(non_vacant) == 1  # exactly one identification-triggering event, not three
