"""
Smoke tests against the REAL application (backend/app), not a mock. These
prove the thing you actually unzip and run works end to end: real
persistence, real admin auth (now enforced on every protected route via
require_admin), real YOLO person detection, and the real face-matching
pipeline wired together.

This is a smaller, focused suite by design -- the 476-test suite in
../design-acceptance-suite/ already covers the full API contract and
business-logic surface exhaustively against a lightweight mock. This suite
answers a different, narrower question: "does the actual shipped code
really run?"
"""
import io


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_health_check(client):
    # The health check is deliberately the one route left open (no auth
    # required) -- standard practice for an uptime probe endpoint.
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "workstation-monitoring"}


def test_protected_route_rejects_missing_auth(client):
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101})
    assert resp.status_code == 401


def test_protected_route_rejects_garbage_token(client):
    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101}, headers=auth("not-a-real-token"))
    assert resp.status_code == 401


def test_default_admin_seeded_and_can_login(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "TestPassword123!"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "SUPER_ADMIN"
    assert len(resp.json()["session_token"]) > 20


def test_wrong_password_rejected(client):
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    assert resp.status_code == 401


def test_account_lockout_after_five_failures(client):
    for _ in range(5):
        client.post("/admin/login", json={"username": "hr_admin1", "password": "wrong"})
    resp = client.post("/admin/login", json={"username": "hr_admin1", "password": "TestPassword123!"})
    assert resp.status_code == 429


def test_session_check_after_login(client, admin_token):
    resp = client.get("/admin/session", headers=auth(admin_token))
    assert resp.json()["valid"] is True


def test_save_and_check_workstation(client, admin_token):
    resp = client.post("/workstations/save", headers=auth(admin_token), json={
        "org_id": 1, "cam_id": 101,
        "workstations": [{"name": "Desk-A", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    assert resp.status_code == 200

    resp = client.get("/workstations/check", params={"org_id": 1, "cam_id": 101}, headers=auth(admin_token))
    body = resp.json()
    assert body["has_workstations"] is True
    assert body["workstations"][0]["name"] == "Desk-A"


def test_workstation_persists_across_requests_same_session(client, admin_token):
    # Proves this is a real database, not a per-request in-memory dict.
    client.post("/workstations/save", headers=auth(admin_token), json={
        "org_id": 2, "cam_id": 55, "workstations": [{"name": "Desk-Z", "x1": 0, "y1": 0, "x2": 1, "y2": 1}],
    })
    resp1 = client.get("/workstations/check", params={"org_id": 2, "cam_id": 55}, headers=auth(admin_token))
    resp2 = client.get("/workstations/check", params={"org_id": 2, "cam_id": 55}, headers=auth(admin_token))
    assert resp1.json() == resp2.json()
    assert resp1.json()["count"] == 1


def test_enroll_employee_with_real_photo(client, admin_token, sample_photo_path):
    with open(sample_photo_path, "rb") as f:
        resp = client.post(
            "/employees/enroll", headers=auth(admin_token),
            data={"org_id": 1, "employee_id": "EMP-SMOKE-1", "name": "Smoke Test Employee",
                  "view": "front", "depth_m": 1.0},
            files={"photo": ("front.jpg", f, "image/jpeg")},
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["employee_id"] == "EMP-SMOKE-1"
    assert "front" in body["views_captured"]
    assert body["face_backend"] in ("stub", "insightface")


def test_enrolled_employee_appears_in_list(client, admin_token, sample_photo_path):
    with open(sample_photo_path, "rb") as f:
        client.post("/employees/enroll", headers=auth(admin_token),
                     data={"org_id": 1, "employee_id": "EMP-SMOKE-2", "name": "Another Employee",
                           "view": "front", "depth_m": 1.0},
                     files={"photo": ("front.jpg", f, "image/jpeg")})
    resp = client.get("/employees/list", params={"org_id": 1}, headers=auth(admin_token))
    ids = [e["employee_id"] for e in resp.json()["employees"]]
    assert "EMP-SMOKE-2" in ids


def test_delete_employee_with_real_history_does_not_crash(client, admin_token, sample_photo_path):
    """Regression test for a real bug found via an end-to-end dry run: the
    original implementation hard-deleted the employee row, which threw a
    FOREIGN KEY IntegrityError the moment that employee had ANY real
    history (a workstation assignment, an attendance record, a shift
    assignment) -- which is the normal case, not an edge case. Fixed by
    soft-deleting (active=False) while hard-deleting only the biometric
    gallery data, matching the documented design intent."""
    h = auth(admin_token)
    with open(sample_photo_path, "rb") as f:
        client.post("/employees/enroll", headers=h,
                    data={"org_id": 1, "employee_id": "EMP-HISTORY", "name": "Has History", "view": "front", "depth_m": 1.0},
                    files={"photo": ("f.jpg", f, "image/jpeg")})

    client.post("/workstations/save", headers=h, json={
        "org_id": 1, "cam_id": 850, "workstations": [{"name": "Desk-Hist", "x1": 0, "y1": 0, "x2": 1, "y2": 1}]})
    client.post("/workstations/assign", headers=h, json={
        "org_id": 1, "cam_id": 850, "workstation_name": "Desk-Hist", "employee_id": "EMP-HISTORY", "effective_from": "2026-01-01"})

    shift_id = client.post("/shifts", headers=h, json={
        "org_id": 1, "shift_name": "Test Shift", "start_time": "09:00", "end_time": "18:00"}).json()["shift_id"]
    client.post("/employees/EMP-HISTORY/shift-assignment", headers=h,
                json={"org_id": 1, "shift_id": shift_id, "effective_from": "2026-01-01"})

    client.post("/attendance/record_detection", headers=h,
                json={"org_id": 1, "employee_id": "EMP-HISTORY", "timestamp": "2026-09-09T09:00:00"})

    resp = client.request("DELETE", "/employees/delete", headers=h, json={"org_id": 1, "employee_id": "EMP-HISTORY"})
    assert resp.status_code == 200, resp.text  # previously: 500 IntegrityError

    listed = client.get("/employees/list", params={"org_id": 1}, headers=h)
    assert not any(e["employee_id"] == "EMP-HISTORY" for e in listed.json()["employees"])

    listed_all = client.get("/employees/list", params={"org_id": 1, "include_inactive": True}, headers=h)
    match = next(e for e in listed_all.json()["employees"] if e["employee_id"] == "EMP-HISTORY")
    assert match["active"] is False

    today = client.get("/attendance/today", params={"org_id": 1, "employee_id": "EMP-HISTORY"}, headers=h)
    assert today.status_code == 200  # historical attendance record was not deleted


def test_full_detection_pipeline_end_to_end(client, admin_token, sample_photo_path):
    """The centerpiece smoke test: real employee enrollment, real
    workstation, real assignment, real YOLO person detection, real face
    matching, and a real persisted identity event -- exercised together
    exactly as a user clicking through the frontend would trigger them."""
    with open(sample_photo_path, "rb") as f:
        enroll = client.post("/employees/enroll", headers=auth(admin_token),
                              data={"org_id": 1, "employee_id": "EMP-PIPELINE", "name": "Pipeline Test",
                                    "view": "front", "depth_m": 1.0},
                              files={"photo": ("front.jpg", f, "image/jpeg")})
    assert enroll.status_code == 201

    ws = client.post("/workstations/save", headers=auth(admin_token), json={
        "org_id": 1, "cam_id": 900, "workstations": [{"name": "Pipeline-Desk", "x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0}],
    })
    assert ws.status_code == 200

    assign = client.post("/workstations/assign", headers=auth(admin_token), json={
        "org_id": 1, "cam_id": 900, "workstation_name": "Pipeline-Desk",
        "employee_id": "EMP-PIPELINE", "effective_from": "2026-09-06",
    })
    assert assign.status_code == 200

    with open(sample_photo_path, "rb") as f:
        detect = client.post("/workstations/simulate_detection", headers=auth(admin_token),
                              data={"org_id": 1, "cam_id": 900, "workstation_name": "Pipeline-Desk"},
                              files={"frame": ("frame.jpg", f, "image/jpeg")})
    assert detect.status_code == 200
    body = detect.json()
    assert body["people_detected"] >= 1  # the sample photo contains real people
    assert body["occupied"] is True
    assert body["event_type"] in ("MATCH", "MISMATCH", "UNKNOWN")

    status = client.get("/workstations/identity_status", params={"org_id": 1, "cam_id": 900}, headers=auth(admin_token))
    ws_status = status.json()["workstations"][0]
    assert ws_status["match_status"] == body["event_type"]


def test_attendance_record_and_check_today(client, admin_token, sample_photo_path):
    with open(sample_photo_path, "rb") as f:
        client.post("/employees/enroll", headers=auth(admin_token),
                     data={"org_id": 1, "employee_id": "EMP-ATT-1", "name": "Attendance Test",
                           "view": "front", "depth_m": 1.0},
                     files={"photo": ("front.jpg", f, "image/jpeg")})

    resp = client.post("/attendance/record_detection", headers=auth(admin_token), json={
        "org_id": 1, "employee_id": "EMP-ATT-1", "location": "Desk-A",
    })
    assert resp.status_code == 201
    assert resp.json()["status"] == "PRESENT"

    today = client.get("/attendance/today", params={"org_id": 1, "employee_id": "EMP-ATT-1"}, headers=auth(admin_token))
    assert today.status_code == 200
    assert today.json()["status"] == "PRESENT"


def test_scheduled_break_actually_applies_through_real_endpoint(client, admin_token, sample_photo_path):
    """Regression test for a real gap found on audit: record_detection()
    previously always called next_attendance_status() with
    break_windows=[], so ON_BREAK could never be reached even though
    shifts/break_schedules had working CRUD and the underlying state
    machine was fully correct. This proves the real lookup is wired up."""
    h = auth(admin_token)
    with open(sample_photo_path, "rb") as f:
        client.post("/employees/enroll", headers=h,
                     data={"org_id": 1, "employee_id": "EMP-BREAK", "name": "Break Test",
                           "view": "front", "depth_m": 1.0},
                     files={"photo": ("front.jpg", f, "image/jpeg")})

    shift_id = client.post("/shifts", headers=h, json={
        "org_id": 1, "shift_name": "Day Shift", "start_time": "09:00", "end_time": "18:00",
    }).json()["shift_id"]
    client.post("/breaks", headers=h, json={
        "org_id": 1, "shift_id": shift_id, "break_name": "Lunch",
        "start_time": "13:00", "end_time": "13:45",
    })
    client.post("/employees/EMP-BREAK/shift-assignment", headers=h, json={
        "org_id": 1, "shift_id": shift_id, "effective_from": "2020-01-01",
    })

    # First detection in the morning -> PRESENT
    morning = client.post("/attendance/record_detection", headers=h, json={
        "org_id": 1, "employee_id": "EMP-BREAK", "timestamp": "2026-09-07T09:00:00",
    })
    assert morning.json()["status"] == "PRESENT"

    # A later detection during the configured lunch window -> ON_BREAK, for real
    lunch = client.post("/attendance/record_detection", headers=h, json={
        "org_id": 1, "employee_id": "EMP-BREAK", "timestamp": "2026-09-07T13:15:00",
    })
    assert lunch.json()["status"] == "ON_BREAK"

    # Back at the desk after lunch -> PRESENT again
    afternoon = client.post("/attendance/record_detection", headers=h, json={
        "org_id": 1, "employee_id": "EMP-BREAK", "timestamp": "2026-09-07T14:00:00",
    })
    assert afternoon.json()["status"] == "PRESENT"


def test_shifts_and_breaks_real_crud(client, admin_token):
    shift = client.post("/shifts", headers=auth(admin_token),
                         json={"org_id": 1, "shift_name": "Morning", "start_time": "09:00", "end_time": "18:00"})
    assert shift.status_code == 201
    shift_id = shift.json()["shift_id"]

    brk = client.post("/breaks", headers=auth(admin_token),
                       json={"org_id": 1, "shift_id": shift_id, "break_name": "Lunch",
                             "start_time": "13:00", "end_time": "13:45"})
    assert brk.status_code == 201

    listed = client.get("/shifts", params={"org_id": 1}, headers=auth(admin_token))
    assert any(s["shift_id"] == shift_id for s in listed.json()["shifts"])


def test_dataset_upload_and_validate_real_zip(client, admin_token):
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # A structurally valid but intentionally incomplete dataset (missing
        # 'top' view) to prove real validation logic runs, not a stub.
        for view in ("front", "left", "right"):
            zf.writestr(f"EMP-9001/depth_001m/{view}.jpg", b"fake-image-bytes")
    buf.seek(0)

    resp = client.post("/dataset/upload", headers=auth(admin_token), data={"org_id": 1},
                        files={"file": ("dataset.zip", buf, "application/zip")})
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]

    validate = client.get(f"/dataset/validate/{upload_id}", headers=auth(admin_token))
    assert validate.status_code == 200
    body = validate.json()
    assert body["summary"]["people_found"] == 1
    assert any("top.jpg" in w["issue"] for w in body["warnings"])


def test_system_health_reports_face_backend(client, admin_token):
    resp = client.get("/system/health", params={"org_id": 1}, headers=auth(admin_token))
    assert resp.status_code == 200
    assert resp.json()["face_recognition_backend"] in ("stub", "insightface")
