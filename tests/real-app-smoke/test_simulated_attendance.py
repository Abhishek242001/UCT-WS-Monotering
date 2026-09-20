"""
Verifies item 8: Video Analysis (StreamWorker.is_live=False) writes to
the genuinely separate SimulatedAttendance / SimulatedAttendanceSegment
tables instead of writing nothing at all (the previous, safer-but-blunter
behavior from an earlier round of this work) -- and confirms a live
stream never writes to the simulated tables, the same way Video Analysis
never writes to the real ones.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_video_analysis_writes_simulated_attendance_not_real(client, admin_token, synthetic_video, sample_photo_path):
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SIM-1", "name": "Simulated Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 600, "workstations": [{"name": "SimDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 600, "workstation_name": "SimDesk",
        "employee_id": "EMP-SIM-1", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=600, poll_interval_seconds=0)  # is_live defaults False
    worker.start()
    worker.join(timeout=60)

    real_resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-SIM-1"})
    assert real_resp.status_code == 404, "Video Analysis must never write to the REAL attendance table"

    sim_resp = client.get("/attendance/simulated/today", headers=h, params={"org_id": 1, "employee_id": "EMP-SIM-1"})
    assert sim_resp.status_code == 200, "Video Analysis should write to the SIMULATED table"
    assert sim_resp.json()["status"] == "PRESENT"


def test_live_stream_never_writes_simulated_attendance(client, admin_token, synthetic_video, sample_photo_path):
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SIM-2", "name": "Live Not Simulated", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 601, "workstations": [{"name": "LiveDesk2", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 601, "workstation_name": "LiveDesk2",
        "employee_id": "EMP-SIM-2", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=601, poll_interval_seconds=0, is_live=True)
    worker.start()
    worker.join(timeout=60)

    real_resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-SIM-2"})
    assert real_resp.status_code == 200, "a live stream should write to the REAL attendance table"

    sim_resp = client.get("/attendance/simulated/today", headers=h, params={"org_id": 1, "employee_id": "EMP-SIM-2"})
    assert sim_resp.status_code == 404, "a live stream must never write to the SIMULATED table"


def test_simulated_segments_close_on_location_change_same_as_real(client, admin_token):
    """The segment open/close-on-location-change fix from item 6 applies
    equally to the simulated path -- same _close_open_segment_and_open_new
    helper, just parameterized to a different table."""
    from datetime import datetime, timedelta
    from app.database import SessionLocal
    from app.routers.attendance import record_simulated_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SIM-3", "name": "Simulated Segment Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg"), "rb"), "image/jpeg")})

    db = SessionLocal()
    t0 = datetime(2026, 2, 1, 9, 0, 0)
    record_simulated_detection_core(db, 1, "EMP-SIM-3", cam_id=100, location="DeskA", timestamp=t0)
    record_simulated_detection_core(db, 1, "EMP-SIM-3", cam_id=100, location="DeskB", timestamp=t0 + timedelta(seconds=180))
    db.close()

    segs = client.get("/attendance/simulated/segments", headers=h,
                       params={"org_id": 1, "employee_id": "EMP-SIM-3", "date": "2026-02-01"}).json()["segments"]
    segs = sorted(segs, key=lambda s: s["start_time"])
    assert len(segs) == 2
    assert segs[0]["duration_seconds"] == 180
    assert segs[0]["end_time"] is not None
    assert segs[1]["end_time"] is None
