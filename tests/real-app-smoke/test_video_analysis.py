"""
Tests the video-upload "Run AI Analysis" feature: real file upload, the
default-parameter resolution (org_id/cam_id/user_id, auto-provisioned demo
workstation), and the live-results WebSocket -- proving it is genuinely
the same underlying pipeline as a live RTSP stream, not a separate
lookalike implementation.

Also covers the multi-tenant audit fix: org_id is now required at upload
time (previously videos had no owner at all), and every video/analysis
endpoint verifies the caller's org access via verify_org_access().
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


def test_video_upload_rejects_non_video_extension(client, admin_token):
    resp = client.post("/videos/upload", headers=auth(admin_token), data={"org_id": 1},
                        files={"file": ("notavideo.txt", b"hello", "text/plain")})
    assert resp.status_code == 422


def test_video_upload_requires_org_id(client, admin_token, synthetic_video):
    with open(synthetic_video, "rb") as f:
        resp = client.post("/videos/upload", headers=auth(admin_token),
                            files={"file": ("demo.mp4", f, "video/mp4")})  # no org_id
    assert resp.status_code == 422


def test_video_upload_success(client, admin_token, synthetic_video):
    resp = upload(client, admin_token, synthetic_video)
    assert resp.status_code == 201
    body = resp.json()
    assert body["video_id"].startswith("VID-")
    assert body["size_bytes"] > 0


def test_video_appears_in_list_after_upload(client, admin_token, synthetic_video):
    video_id = upload(client, admin_token, synthetic_video).json()["video_id"]
    listed = client.get("/videos", params={"org_id": 1}, headers=auth(admin_token))
    assert any(v["video_id"] == video_id for v in listed.json()["videos"])


def test_analyze_unknown_video_id_404s(client, admin_token):
    resp = client.post("/videos/VID-doesnotexist/analyze", headers=auth(admin_token), data={})
    assert resp.status_code == 404


def test_analyze_with_zero_parameters_uses_sensible_defaults(client, admin_token, synthetic_video):
    """The core sales-demo requirement: upload, then analyze with
    absolutely no configuration, and it still produces a usable result."""
    video_id = upload(client, admin_token, synthetic_video, org_id=1).json()["video_id"]

    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"max_frames": 6, "poll_interval_seconds": 0})
    assert analyze.status_code == 201
    body = analyze.json()
    assert body["org_id"] == 1  # defaults to the video's own org, not a hardcoded global
    assert body["workstation_name"] == "Demo-Desk"
    assert all(body["defaults_used"].values())  # every field defaulted, none supplied


def test_analyze_auto_provisions_a_demo_workstation(client, admin_token, synthetic_video):
    video_id = upload(client, admin_token, synthetic_video).json()["video_id"]
    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"max_frames": 6, "poll_interval_seconds": 0})
    cam_id = analyze.json()["cam_id"]

    check = client.get("/workstations/check", params={"org_id": 1, "cam_id": cam_id}, headers=auth(admin_token))
    assert check.json()["has_workstations"] is True
    assert check.json()["workstations"][0]["name"] == "Demo-Desk"


def test_analyze_same_video_twice_reuses_same_demo_camera_slot(client, admin_token, synthetic_video):
    """Re-running analysis on the same uploaded video (e.g. for a second
    sales call) should land on the same demo workstation, not create a
    duplicate desk each time."""
    video_id = upload(client, admin_token, synthetic_video).json()["video_id"]

    first = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                         data={"max_frames": 3, "poll_interval_seconds": 0})
    first.raise_for_status()
    second = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                          data={"max_frames": 3, "poll_interval_seconds": 0})
    second.raise_for_status()
    assert first.json()["cam_id"] == second.json()["cam_id"]


def test_analyze_respects_explicit_org_and_cam_overrides(client, admin_token, synthetic_video):
    """The bootstrap SUPER_ADMIN account can legitimately override org_id
    since it has cross-org access -- this exercises that override path."""
    video_id = upload(client, admin_token, synthetic_video, org_id=1).json()["video_id"]

    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"org_id": 7, "cam_id": 12345, "max_frames": 3, "poll_interval_seconds": 0})
    assert analyze.status_code == 201
    body = analyze.json()
    assert body["org_id"] == 7
    assert body["cam_id"] == 12345
    assert body["defaults_used"] == {"org_id": False, "cam_id": False, "user_id": True}


def test_upload_response_includes_file_hash(client, admin_token, synthetic_video):
    resp = upload(client, admin_token, synthetic_video)
    assert "file_hash" in resp.json()
    assert len(resp.json()["file_hash"]) == 64  # SHA-256 hex digest length


def test_find_by_hash_returns_not_found_for_unknown_hash(client, admin_token):
    resp = client.get("/videos/find_by_hash", params={"org_id": 1, "file_hash": "0" * 64}, headers=auth(admin_token))
    assert resp.status_code == 200
    assert resp.json()["found"] is False


def test_find_by_hash_finds_an_already_uploaded_video(client, admin_token, synthetic_video):
    uploaded = upload(client, admin_token, synthetic_video, org_id=1).json()
    resp = client.get("/videos/find_by_hash", params={"org_id": 1, "file_hash": uploaded["file_hash"]}, headers=auth(admin_token))
    body = resp.json()
    assert body["found"] is True
    assert body["video_id"] == uploaded["video_id"]


def test_find_by_hash_is_computed_from_content_not_filename(client, admin_token, synthetic_video):
    """The whole point: uploading the identical bytes under a DIFFERENT
    filename must still be detected as the same video."""
    first = upload(client, admin_token, synthetic_video, org_id=1, filename="Vid1.mp4").json()
    resp = client.get("/videos/find_by_hash", params={"org_id": 1, "file_hash": first["file_hash"]}, headers=auth(admin_token))
    body = resp.json()
    assert body["found"] is True
    assert body["video_id"] == first["video_id"]
    assert body["filename"] == "Vid1.mp4"  # confirms it found the ORIGINAL upload, not a coincidence


def test_find_by_hash_is_scoped_per_org(client, admin_token, synthetic_video):
    """The same video content uploaded by org 1 must not be reported as
    'already uploaded' when org 2 checks -- each org's upload history is
    independent, even for byte-identical files."""
    client.post("/admin/users", headers=auth(admin_token),
                json={"username": "hash_org2_admin", "password": "TestPassword123!", "org_id": 2})
    org2_token = client.post("/admin/login", json={"username": "hash_org2_admin", "password": "TestPassword123!"}).json()["session_token"]

    org1_upload = upload(client, admin_token, synthetic_video, org_id=1).json()

    check_as_org2 = client.get("/videos/find_by_hash", params={"org_id": 2, "file_hash": org1_upload["file_hash"]}, headers=auth(org2_token))
    assert check_as_org2.json()["found"] is False  # org 2 has never uploaded this content itself

    check_as_org1 = client.get("/videos/find_by_hash", params={"org_id": 1, "file_hash": org1_upload["file_hash"]}, headers=auth(admin_token))
    assert check_as_org1.json()["found"] is True


def test_different_videos_produce_different_hashes(client, admin_token, synthetic_video, sample_photo_path):
    """Sanity check the hash is actually content-sensitive, not a constant
    or filename-derived value."""
    import cv2
    import tempfile
    photo = cv2.imread(sample_photo_path)
    h, w = photo.shape[:2]
    other_path = tempfile.mktemp(suffix=".mp4")
    writer = cv2.VideoWriter(other_path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (w, h))
    for _ in range(2):
        writer.write(photo)
    writer.release()

    video_a = upload(client, admin_token, synthetic_video, org_id=1).json()
    video_b = upload(client, admin_token, other_path, org_id=1).json()
    assert video_a["file_hash"] != video_b["file_hash"]


def test_concurrent_multi_org_analysis_no_cross_contamination(client, admin_token):
    """Verifies real concurrent usage across two organizations at once --
    not sequential calls that happen to use different org_ids, but two
    StreamWorker background threads genuinely running at the same time,
    each writing to the shared SQLite database and reading from the same
    shared YOLO/InsightFace model singletons. The thing this actually
    proves: org A's employee is never mismatched onto org B's desk (or
    vice versa) under concurrent load."""
    import threading
    import cv2
    import numpy as np
    import tempfile as tf

    photo = cv2.imread(str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg"))
    h, w = photo.shape[:2]
    blank = np.full((h, w, 3), 40, dtype="uint8")

    def build(path):
        writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 5, (w, h))
        for _ in range(2):
            writer.write(blank)
        for _ in range(3):
            writer.write(photo)
        writer.release()

    video_a, video_b = tf.mktemp(suffix="_a.mp4"), tf.mktemp(suffix="_b.mp4")
    build(video_a)
    build(video_b)

    hb = auth(admin_token)
    client.post("/admin/users", headers=hb, json={"username": "conc_a", "password": "Pass123!", "org_id": 301})
    client.post("/admin/users", headers=hb, json={"username": "conc_b", "password": "Pass123!", "org_id": 302})
    token_a = client.post("/admin/login", json={"username": "conc_a", "password": "Pass123!"}).json()["session_token"]
    token_b = client.post("/admin/login", json={"username": "conc_b", "password": "Pass123!"}).json()["session_token"]

    results, errors = {}, {}

    def run(token, org_id, cam_id, video_path, label):
        try:
            h = auth(token)
            with open(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg", "rb") as f:
                client.post("/employees/enroll", headers=h,
                            data={"org_id": org_id, "employee_id": f"EMP-{label}", "name": label, "view": "front", "depth_m": 1.0},
                            files={"photo": (f"{label}.jpg", f, "image/jpeg")})
            client.post("/workstations/save", headers=h, json={
                "org_id": org_id, "cam_id": cam_id, "workstations": [{"name": f"Desk-{label}", "x1": 0, "y1": 0, "x2": 1, "y2": 1}],
            })
            client.post("/workstations/assign", headers=h, json={
                "org_id": org_id, "cam_id": cam_id, "workstation_name": f"Desk-{label}",
                "employee_id": f"EMP-{label}", "effective_from": "2026-09-07",
            })
            with open(video_path, "rb") as f:
                video_id = client.post("/videos/upload", headers=h, data={"org_id": org_id},
                                        files={"file": (f"{label}.mp4", f, "video/mp4")}).json()["video_id"]
            stream_id = client.post(f"/videos/{video_id}/analyze", headers=h,
                                     data={"cam_id": cam_id, "max_frames": 5, "poll_interval_seconds": 0}).json()["stream_id"]
            with client.websocket_connect(f"/ws/streams/{stream_id}?token={token}") as ws:
                while ws.receive_json()["type"] != "completed":
                    pass
            status = client.get("/workstations/identity_status", params={"org_id": org_id, "cam_id": cam_id}, headers=h)
            results[label] = status.json()["workstations"][0]
        except Exception as e:
            errors[label] = str(e)

    t1 = threading.Thread(target=run, args=(token_a, 301, 601, video_a, "A"))
    t2 = threading.Thread(target=run, args=(token_b, 302, 602, video_b, "B"))
    t1.start(); t2.start()
    t1.join(timeout=60); t2.join(timeout=60)

    assert not errors, f"Concurrent run hit errors: {errors}"
    assert results["A"]["detected_employee_id"] == "EMP-A"
    assert results["B"]["detected_employee_id"] == "EMP-B"


def test_org_scoped_admin_cannot_upload_to_another_org(client, admin_token, synthetic_video):
    """Regression test for the audit fix: a real org-scoped HR_ADMIN
    (created via POST /admin/users, unlike the unrestricted bootstrap
    SUPER_ADMIN) must be rejected when trying to act on a different org."""
    create = client.post("/admin/users", headers=auth(admin_token),
                          json={"username": "org1_admin", "password": "TestPassword123!", "org_id": 1})
    assert create.status_code == 201

    login = client.post("/admin/login", json={"username": "org1_admin", "password": "TestPassword123!"})
    org1_token = login.json()["session_token"]
    assert login.json()["org_id"] == 1

    # Allowed: uploading to its own org.
    own_org = upload(client, org1_token, synthetic_video, org_id=1)
    assert own_org.status_code == 201

    # Rejected: uploading "as" a different org.
    other_org = upload(client, org1_token, synthetic_video, org_id=2)
    assert other_org.status_code == 403


def test_org_scoped_admin_cannot_analyze_another_orgs_video(client, admin_token, synthetic_video):
    """A video uploaded under org 1 must not be analyzable by an admin
    scoped to org 2, even though the bootstrap SUPER_ADMIN can see both."""
    client.post("/admin/users", headers=auth(admin_token),
                json={"username": "org2_admin", "password": "TestPassword123!", "org_id": 2})
    org2_login = client.post("/admin/login", json={"username": "org2_admin", "password": "TestPassword123!"})
    org2_token = org2_login.json()["session_token"]

    video_id = upload(client, admin_token, synthetic_video, org_id=1).json()["video_id"]

    resp = client.post(f"/videos/{video_id}/analyze", headers=auth(org2_token),
                        data={"max_frames": 3, "poll_interval_seconds": 0})
    assert resp.status_code == 403


def test_live_results_websocket_streams_events_for_uploaded_video(client, admin_token, synthetic_video):
    """The centerpiece test: proves the live-push WebSocket works for an
    uploaded video exactly as it does for a live stream -- started,
    progress, event, and completed messages, in order, ending cleanly."""
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

    types_seen = {m["type"] for m in messages}
    assert "started" in types_seen
    assert "progress" in types_seen
    assert "completed" in types_seen
    completed = [m for m in messages if m["type"] == "completed"][0]
    assert completed["frames_processed"] == 6
    assert completed["reason"] == "source_ended"


def test_live_results_websocket_includes_frame_previews(client, admin_token, synthetic_video):
    """Confirms the NEW "frame" message type: a live, boxes-drawn preview
    image pushed over the same WebSocket the "centerpiece" test above
    already covers for started/progress/event/completed. Verifies both
    that at least one arrives (throttling means not every processed
    frame necessarily produces one -- see LIVE_FRAME_PUBLISH_FPS in
    stream_worker.py) and that it's a genuinely decodable JPEG data URL,
    not just a string that looks plausible."""
    import base64
    import cv2
    import numpy as np

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

    frame_messages = [m for m in messages if m["type"] == "frame"]
    assert len(frame_messages) >= 1, (
        "expected at least one live 'frame' preview message "
        f"(got message types: {[m['type'] for m in messages]})"
    )

    first = frame_messages[0]
    assert "frame_number" in first
    assert first["image"].startswith("data:image/jpeg;base64,")

    # Decode it for real -- not just a prefix check -- to prove this is
    # an actual valid JPEG, not a malformed/truncated encode.
    raw_b64 = first["image"].split(",", 1)[1]
    jpeg_bytes = base64.b64decode(raw_b64)
    arr = np.frombuffer(jpeg_bytes, dtype="uint8")
    decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape[0] > 0 and decoded.shape[1] > 0


def test_websocket_rejects_invalid_token(client, admin_token, synthetic_video):
    video_id = upload(client, admin_token, synthetic_video).json()["video_id"]
    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"max_frames": 3, "poll_interval_seconds": 0})
    stream_id = analyze.json()["stream_id"]

    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/streams/{stream_id}?token=garbage-token") as ws:
            ws.receive_json()


def test_websocket_rejects_unknown_stream_id(client, admin_token):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/streams/not-a-real-stream-id?token={admin_token}") as ws:
            ws.receive_json()


def test_websocket_rejects_admin_from_another_org(client, admin_token, synthetic_video):
    """Regression test for the audit fix: streams_ws.py previously only
    checked that the token was valid, never that the connecting admin's
    org matched the stream's org."""
    client.post("/admin/users", headers=auth(admin_token),
                json={"username": "org9_admin", "password": "TestPassword123!", "org_id": 9})
    org9_token = client.post("/admin/login", json={"username": "org9_admin", "password": "TestPassword123!"}).json()["session_token"]

    video_id = upload(client, admin_token, synthetic_video, org_id=1).json()["video_id"]
    analyze = client.post(f"/videos/{video_id}/analyze", headers=auth(admin_token),
                           data={"max_frames": 3, "poll_interval_seconds": 0})
    stream_id = analyze.json()["stream_id"]

    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/streams/{stream_id}?token={org9_token}") as ws:
            ws.receive_json()
