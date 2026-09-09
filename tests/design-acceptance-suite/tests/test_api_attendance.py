import pytest
from datetime import datetime, timezone


def record_detection(client, org_id, employee_id, timestamp=None):
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return client.post("/attendance/_test_record_detection", json={
        "org_id": org_id, "employee_id": employee_id, "timestamp": ts,
    })


def test_attendance_today_404_before_any_detection(client, enrolled_employee):
    resp = client.get("/attendance/today", params={"org_id": 1, "employee_id": "EMP-1001"})
    assert resp.status_code == 404


def test_attendance_today_after_detection(client, enrolled_employee):
    record_detection(client, 1, "EMP-1001")
    resp = client.get("/attendance/today", params={"org_id": 1, "employee_id": "EMP-1001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "PRESENT"
    assert body["sign_out_time"] is None


def test_repeated_detections_same_day_do_not_change_sign_in_time(client, enrolled_employee):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record_detection(client, 1, "EMP-1001", timestamp=f"{today}T07:15:03")
    record_detection(client, 1, "EMP-1001", timestamp=f"{today}T09:00:00")
    resp = client.get("/attendance/today", params={"org_id": 1, "employee_id": "EMP-1001"})
    assert resp.json()["sign_in_time"] == "07:15:03"


def test_attendance_report_empty_for_new_employee(client, enrolled_employee):
    resp = client.get("/attendance/report", params={
        "org_id": 1, "employee_id": "EMP-1001", "from_": "2026-09-01", "to": "2026-09-06",
    })
    assert resp.json()["days"] == []


def test_attendance_report_includes_recorded_days(client, enrolled_employee):
    record_detection(client, 1, "EMP-1001", timestamp="2026-09-05T07:00:00")
    resp = client.get("/attendance/report", params={"org_id": 1, "employee_id": "EMP-1001"})
    assert len(resp.json()["days"]) == 1


def test_attendance_segments_returns_expected_shape(client, enrolled_employee):
    resp = client.get("/attendance/segments", params={"org_id": 1, "employee_id": "EMP-1001", "date": "2026-09-06"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["employee_id"] == "EMP-1001"
    assert "segments" in body


VALID_EXCEPTION = {
    "org_id": 1, "employee_id": "EMP-1001", "date": "2026-09-06",
    "type": "FIELD_WORK", "reason": "Client visit", "admin_user": "hr_admin1",
}


def test_record_exception_success(client):
    resp = client.post("/attendance/exception", json=VALID_EXCEPTION)
    assert resp.status_code == 201
    assert resp.json()["exception_id"].startswith("EXC-")


@pytest.mark.parametrize("exc_type", ["FIELD_WORK", "LEAVE", "WFH", "CORRECTION"])
def test_record_exception_accepts_all_valid_types(client, exc_type):
    resp = client.post("/attendance/exception", json=dict(VALID_EXCEPTION, type=exc_type))
    assert resp.status_code == 201


@pytest.mark.parametrize("bad_type", ["VACATION", "SICK", "REMOTE", "", "field_work"])
def test_record_exception_rejects_invalid_type(client, bad_type):
    resp = client.post("/attendance/exception", json=dict(VALID_EXCEPTION, type=bad_type))
    assert resp.status_code == 422


@pytest.mark.parametrize("missing_field", ["org_id", "employee_id", "date", "type", "reason", "admin_user"])
def test_record_exception_missing_field_rejected(client, missing_field):
    payload = dict(VALID_EXCEPTION)
    del payload[missing_field]
    resp = client.post("/attendance/exception", json=payload)
    assert resp.status_code == 422


def test_exception_ids_are_unique(client):
    id1 = client.post("/attendance/exception", json=VALID_EXCEPTION).json()["exception_id"]
    id2 = client.post("/attendance/exception", json=VALID_EXCEPTION).json()["exception_id"]
    assert id1 != id2


@pytest.mark.parametrize("employee_id", ["EMP-A", "EMP-B", "EMP-C"])
def test_attendance_today_isolated_per_employee(client, employee_id):
    record_detection(client, 1, employee_id)
    for other in ["EMP-A", "EMP-B", "EMP-C"]:
        resp = client.get("/attendance/today", params={"org_id": 1, "employee_id": other})
        if other == employee_id:
            assert resp.status_code == 200
        else:
            assert resp.status_code == 404


def test_attendance_isolated_per_org(client):
    record_detection(client, 1, "EMP-1001")
    resp = client.get("/attendance/today", params={"org_id": 2, "employee_id": "EMP-1001"})
    assert resp.status_code == 404
