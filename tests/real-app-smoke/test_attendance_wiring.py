"""
Verifies the attendance/analytics wiring: app/routers/attendance.py's
record_detection logic extracted into record_detection_core() so
stream_worker.py can call it directly on every confirmed MATCH; the
previously-never-closing AttendanceSegment fixed to actually set
end_time/duration_seconds; and -- the most safety-critical part of this
change -- the new is_live flag that must be True for a real live camera
stream and False for a Video Analysis run, since Video Analysis processes
uploaded demo footage and must NEVER write real attendance data into the
same tables real reporting reads from.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
SAMPLE_PHOTO = str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# record_detection_core: segment open/extend/close behavior
# ---------------------------------------------------------------------------

def test_record_detection_core_extends_segment_at_same_location_not_fragments(client, admin_token):
    from app.database import SessionLocal
    from app.routers.attendance import record_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SEG-1", "name": "Segment Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    db = SessionLocal()
    t0 = datetime(2026, 1, 5, 9, 0, 0)
    record_detection_core(db, 1, "EMP-SEG-1", cam_id=100, location="DeskA", timestamp=t0)
    record_detection_core(db, 1, "EMP-SEG-1", cam_id=100, location="DeskA", timestamp=t0 + timedelta(seconds=60))
    record_detection_core(db, 1, "EMP-SEG-1", cam_id=100, location="DeskA", timestamp=t0 + timedelta(seconds=120))
    db.close()

    resp = client.get("/attendance/segments", headers=h, params={"org_id": 1, "employee_id": "EMP-SEG-1", "date": "2026-01-05"})
    segments = resp.json()["segments"]
    assert len(segments) == 1, f"three detections at the SAME location should stay ONE segment, got: {segments}"
    assert segments[0]["end_time"] is None  # still open -- never left


def test_record_detection_core_closes_segment_on_location_change(client, admin_token):
    from app.database import SessionLocal
    from app.routers.attendance import record_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SEG-2", "name": "Segment Test 2", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    db = SessionLocal()
    t0 = datetime(2026, 1, 5, 9, 0, 0)
    record_detection_core(db, 1, "EMP-SEG-2", cam_id=100, location="DeskA", timestamp=t0)
    record_detection_core(db, 1, "EMP-SEG-2", cam_id=100, location="DeskB", timestamp=t0 + timedelta(seconds=300))
    db.close()

    resp = client.get("/attendance/segments", headers=h, params={"org_id": 1, "employee_id": "EMP-SEG-2", "date": "2026-01-05"})
    segments = sorted(resp.json()["segments"], key=lambda s: s["start_time"])
    assert len(segments) == 2
    assert segments[0]["location"] == "DeskA"
    assert segments[0]["end_time"] is not None
    assert segments[0]["duration_seconds"] == 300
    assert segments[1]["location"] == "DeskB"
    assert segments[1]["end_time"] is None  # still open


def test_close_stale_sessions_closes_the_final_open_segment(client, admin_token):
    from app.database import SessionLocal
    from app.routers.attendance import record_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SEG-3", "name": "Segment Test 3", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    db = SessionLocal()
    old_ts = datetime.utcnow() - timedelta(hours=2)  # older than the default away-timeout
    record_detection_core(db, 1, "EMP-SEG-3", cam_id=100, location="DeskA", timestamp=old_ts)
    db.close()

    resp = client.post("/attendance/close_stale_sessions", headers=h)
    assert resp.status_code == 200
    assert "EMP-SEG-3" in resp.json()["employee_ids"]

    segs = client.get("/attendance/segments", headers=h,
                       params={"org_id": 1, "employee_id": "EMP-SEG-3", "date": old_ts.date().isoformat()}).json()["segments"]
    assert len(segs) == 1
    assert segs[0]["end_time"] is not None, "the final segment should be closed once the session itself is closed out"


# ---------------------------------------------------------------------------
# The critical part: is_live gating. A Video Analysis run (is_live=False)
# must NEVER write attendance data, even on a real, confirmed MATCH.
# ---------------------------------------------------------------------------

def test_live_stream_worker_writes_attendance_on_match(client, admin_token, synthetic_video, sample_photo_path):
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-LIVE-ATTEND", "name": "Live Attendance Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 980, "workstations": [{"name": "LiveAttendDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 980, "workstation_name": "LiveAttendDesk",
        "employee_id": "EMP-LIVE-ATTEND", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=980, poll_interval_seconds=0, is_live=True)
    worker.start()
    worker.join(timeout=60)

    resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-LIVE-ATTEND"})
    assert resp.status_code == 200, "a live stream's confirmed MATCH should have recorded a real attendance row"
    assert resp.json()["status"] == "PRESENT"


def test_video_analysis_stream_worker_does_not_write_attendance(client, admin_token, synthetic_video, sample_photo_path):
    """Same exact scenario as the test above -- same photo, same
    enrollment pattern, a real confirmed MATCH will happen -- except
    is_live is left at its default (False), simulating what Video
    Analysis actually does. No attendance record must be written, even
    though recognition itself still works correctly."""
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-DEMO-NO-ATTEND", "name": "Demo Video Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("front.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 981, "workstations": [{"name": "DemoDesk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 981, "workstation_name": "DemoDesk",
        "employee_id": "EMP-DEMO-NO-ATTEND", "effective_from": "2026-01-01",
    })

    worker = StreamWorker(source=synthetic_video, org_id=1, cam_id=981, poll_interval_seconds=0)  # is_live defaults False
    worker.start()
    worker.join(timeout=60)

    # Prove recognition itself genuinely worked (so a missing attendance
    # record below means the gate worked, not that identification failed
    # for an unrelated reason).
    matched = [r for r in worker._last_identity_by_track.values() if r[0] == "MATCH"]
    assert matched, "expected identification to succeed in this test, to make the absence of an attendance record meaningful"

    resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-DEMO-NO-ATTEND"})
    assert resp.status_code == 404, "a Video Analysis run (is_live=False) must NOT write a real attendance record"


def test_streams_start_endpoint_wires_is_live_true(client, admin_token, synthetic_video):
    from app.routers import streams as streams_router

    h = auth(admin_token)
    resp = client.post("/streams/start", headers=h, json={
        "source": synthetic_video, "org_id": 1, "cam_id": 990, "user_id": 1, "max_frames": 1,
    })
    assert resp.status_code == 201
    stream_id = resp.json()["stream_id"]
    worker = streams_router.get_worker(stream_id)
    assert worker.is_live is True, "/streams/start (a live camera endpoint) must construct its worker with is_live=True"


def test_videos_analyze_endpoint_wires_is_live_false(client, admin_token, synthetic_video):
    from app.routers import streams as streams_router

    h = auth(admin_token)
    with open(synthetic_video, "rb") as f:
        up = client.post("/videos/upload", headers=h, data={"org_id": 1}, files={"file": ("demo.mp4", f, "video/mp4")})
    video_id = up.json()["video_id"]
    analyze = client.post(f"/videos/{video_id}/analyze", headers=h, data={"max_frames": 1})
    stream_id = analyze.json()["stream_id"]
    worker = streams_router.get_worker(stream_id)
    assert worker.is_live is False, "Video Analysis must construct its worker with is_live=False"
