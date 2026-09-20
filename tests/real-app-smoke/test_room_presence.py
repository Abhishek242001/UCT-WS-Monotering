"""
Verifies item 7: a tracked person who is NOT currently occupying any
workstation ROI, but who has a KNOWN identity from a previous confirmed
MATCH (cached in _last_identity_by_track), gets their room presence
recorded too -- using the same record_detection_core() attendance path,
but with location=ROOM_LOCATION instead of a workstation name. Someone
actually at a desk is excluded (already covered as desk time); someone
never identified is excluded (no fresh recognition is run for roaming
people -- see _record_room_presence_for_roaming_people's docstring for
why); Video Analysis (is_live=False) is excluded, same as every other
attendance write in this file.

Test note: the bundled real test photo is static (the same person
position in every frame), so there's no way to make someone genuinely
"walk away from a desk" across a short synthetic video. Most of this
suite tests the room-recording logic directly with a real db session
(same pattern as test_attendance_wiring.py's direct record_detection_core
tests) rather than trying to stage that in a full video; one test drives
the real _process_frame() method directly, with a narrow ROI that the
photo's real detections don't overlap and pre-seeded identity state, to
confirm the wiring INSIDE _process_frame (building occupied_track_ids,
calling the room-presence pass) is correct, not just the standalone
method in isolation.
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
SAMPLE_PHOTO = str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _fake_person(track_id):
    from app.vision import yolo_detector
    return yolo_detector.PersonDetection(confidence=0.9, x1=0.1, y1=0.1, x2=0.3, y2=0.6, track_id=track_id)


# ---------------------------------------------------------------------------
# Direct tests of _record_room_presence_for_roaming_people, with a real db
# session -- verifying real AttendanceSegment rows, not mocked writes.
# ---------------------------------------------------------------------------

def test_roaming_known_person_gets_a_room_segment(client, admin_token):
    from app.database import SessionLocal
    from app.vision.stream_worker import StreamWorker, ROOM_LOCATION

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-ROAM-1", "name": "Roaming Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    worker = StreamWorker(source="", org_id=1, cam_id=500, is_live=True)
    worker._last_identity_by_track[7] = ("MATCH", "EMP-ROAM-1", "Roaming Test", 0.9)

    db = SessionLocal()
    worker._record_room_presence_for_roaming_people(db, [_fake_person(7)], occupied_track_ids=set(), now=1000.0)
    db.close()

    segs = client.get("/attendance/segments", headers=h,
                       params={"org_id": 1, "employee_id": "EMP-ROAM-1", "date": __import__("datetime").datetime.utcnow().date().isoformat()}).json()["segments"]
    assert any(s["location"] == ROOM_LOCATION for s in segs), f"expected a ROOM segment, got: {segs}"


def test_person_currently_at_a_desk_is_excluded_from_room_recording(client, admin_token):
    """Same track_id, same known identity -- but this time it IS in
    occupied_track_ids (i.e. currently at a desk this frame). No ROOM
    record should be written; that time is already desk time."""
    from app.database import SessionLocal
    from app.vision.stream_worker import StreamWorker, ROOM_LOCATION

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-ROAM-2", "name": "Desk Not Roam", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    worker = StreamWorker(source="", org_id=1, cam_id=501, is_live=True)
    worker._last_identity_by_track[8] = ("MATCH", "EMP-ROAM-2", "Desk Not Roam", 0.9)

    db = SessionLocal()
    worker._record_room_presence_for_roaming_people(db, [_fake_person(8)], occupied_track_ids={8}, now=1000.0)
    db.close()

    resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-ROAM-2"})
    assert resp.status_code == 404, "a person currently at a desk should not ALSO get a room record"


def test_never_identified_roaming_person_is_not_recorded(client, admin_token):
    """No fresh recognition is run for roaming people -- only someone
    with an ALREADY-cached identity from a real desk match gets a room
    record. A track with no cached identity at all must be skipped."""
    from app.database import SessionLocal
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    worker = StreamWorker(source="", org_id=1, cam_id=502, is_live=True)
    # worker._last_identity_by_track deliberately left empty

    db = SessionLocal()
    worker._record_room_presence_for_roaming_people(db, [_fake_person(9)], occupied_track_ids=set(), now=1000.0)
    db.close()

    resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-ROAM-2"})
    assert resp.status_code == 404  # not this employee at all -- confirms nothing was fabricated


def test_video_analysis_does_not_record_room_presence(client, admin_token):
    """is_live=False (Video Analysis) must skip room recording, same as
    every other attendance write in this file."""
    from app.database import SessionLocal
    from app.vision.stream_worker import StreamWorker

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-ROAM-3", "name": "Not Live", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    worker = StreamWorker(source="", org_id=1, cam_id=503)  # is_live defaults False
    worker._last_identity_by_track[10] = ("MATCH", "EMP-ROAM-3", "Not Live", 0.9)

    db = SessionLocal()
    worker._record_room_presence_for_roaming_people(db, [_fake_person(10)], occupied_track_ids=set(), now=1000.0)
    db.close()

    resp = client.get("/attendance/today", headers=h, params={"org_id": 1, "employee_id": "EMP-ROAM-3"})
    assert resp.status_code == 404


def test_room_recording_is_heartbeat_gated_per_track(client, admin_token):
    from app.database import SessionLocal
    from app.vision.stream_worker import StreamWorker, ROOM_LOCATION

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-ROAM-4", "name": "Heartbeat Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    worker = StreamWorker(source="", org_id=1, cam_id=504, is_live=True)
    worker._last_identity_by_track[11] = ("MATCH", "EMP-ROAM-4", "Heartbeat Test", 0.9)

    db = SessionLocal()
    worker._record_room_presence_for_roaming_people(db, [_fake_person(11)], occupied_track_ids=set(), now=1000.0)
    worker._record_room_presence_for_roaming_people(db, [_fake_person(11)], occupied_track_ids=set(), now=1000.5)  # well within HEARTBEAT_SECONDS
    db.close()

    date_str = __import__("datetime").datetime.utcnow().date().isoformat()
    segs = client.get("/attendance/segments", headers=h,
                       params={"org_id": 1, "employee_id": "EMP-ROAM-4", "date": date_str}).json()["segments"]
    room_segs = [s for s in segs if s["location"] == ROOM_LOCATION]
    assert len(room_segs) == 1, f"a second call within the heartbeat window should not create a second segment: {room_segs}"


# ---------------------------------------------------------------------------
# End-to-end through the real _process_frame() method, with a narrow ROI
# the photo's real detections don't overlap -- confirms the wiring INSIDE
# _process_frame (building occupied_track_ids, calling the new pass), not
# just the standalone method tested above.
# ---------------------------------------------------------------------------

def test_process_frame_records_room_presence_for_a_desk_no_one_occupies(client, admin_token, sample_photo_path):
    import cv2
    from app.database import SessionLocal
    from app.vision import yolo_detector
    from app.vision.stream_worker import StreamWorker, ROOM_LOCATION

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-ROAM-5", "name": "Process Frame Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(sample_photo_path, "rb"), "image/jpeg")})
    # A tiny ROI in a corner the real photo's detected people don't
    # overlap at all -- so occupied=False for every workstation, and
    # nobody is ever added to occupied_track_ids.
    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 505,
        "workstations": [{"name": "EmptyCornerDesk", "x1": 0.0, "y1": 0.0, "x2": 0.02, "y2": 0.02}],
    })

    worker = StreamWorker(source="", org_id=1, cam_id=505, is_live=True)
    # Pre-seed: this person was already confirmed at some point earlier
    # in the session (this test isn't trying to reproduce that -- it's
    # tested separately throughout this whole round of work). The point
    # here is what _process_frame does NEXT, now that they're roaming.
    worker._last_identity_by_track[1] = ("MATCH", "EMP-ROAM-5", "Process Frame Test", 0.9)

    frame = cv2.imread(sample_photo_path)
    pose_model = yolo_detector.new_model_instance()
    rois = worker._load_rois(SessionLocal())

    db = SessionLocal()
    worker._process_frame(db, frame, rois, pose_model)
    db.close()

    date_str = __import__("datetime").datetime.utcnow().date().isoformat()
    segs = client.get("/attendance/segments", headers=h,
                       params={"org_id": 1, "employee_id": "EMP-ROAM-5", "date": date_str}).json()["segments"]
    assert any(s["location"] == ROOM_LOCATION for s in segs), (
        f"expected _process_frame's real per-frame loop to record room presence "
        f"for a known, roaming person, got: {segs}"
    )
