"""
Verifies the new /attendance/live_now endpoint (item 10): every existing
attendance endpoint required knowing an employee_id up front -- there was
no way to see an org's whole current presence at a glance ("who's here
right now").
"""
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
SAMPLE_PHOTO = str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def _enroll_and_record(client, admin_token, employee_id, org_id=1):
    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": org_id, "employee_id": employee_id, "name": f"Name for {employee_id}",
        "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})
    client.post("/attendance/record_detection", headers=h, json={
        "org_id": org_id, "employee_id": employee_id, "location": "DeskX",
    })


def test_present_employee_appears_in_live_now(client, admin_token):
    h = auth(admin_token)
    _enroll_and_record(client, admin_token, "EMP-LIVE-A")

    resp = client.get("/attendance/live_now", headers=h, params={"org_id": 1})
    assert resp.status_code == 200
    ids = [e["employee_id"] for e in resp.json()["employees"]]
    assert "EMP-LIVE-A" in ids
    entry = next(e for e in resp.json()["employees"] if e["employee_id"] == "EMP-LIVE-A")
    assert entry["name"] == "Name for EMP-LIVE-A"
    assert entry["status"] == "PRESENT"


def test_signed_out_employee_does_not_appear(client, admin_token):
    from app.database import SessionLocal
    from app.models import EmployeeAttendance

    h = auth(admin_token)
    _enroll_and_record(client, admin_token, "EMP-LIVE-B")

    db = SessionLocal()
    rec = db.query(EmployeeAttendance).filter_by(org_id=1, employee_id="EMP-LIVE-B").first()
    rec.status = "SIGNED_OUT"
    db.commit()
    db.close()

    resp = client.get("/attendance/live_now", headers=h, params={"org_id": 1})
    ids = [e["employee_id"] for e in resp.json()["employees"]]
    assert "EMP-LIVE-B" not in ids


def test_employee_never_seen_today_does_not_appear(client, admin_token):
    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-LIVE-C", "name": "Never Recorded", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})
    # deliberately never call record_detection for this one

    resp = client.get("/attendance/live_now", headers=h, params={"org_id": 1})
    ids = [e["employee_id"] for e in resp.json()["employees"]]
    assert "EMP-LIVE-C" not in ids


def test_live_now_is_scoped_per_org(client, admin_token):
    h = auth(admin_token)
    _enroll_and_record(client, admin_token, "EMP-LIVE-ORG1", org_id=1)
    _enroll_and_record(client, admin_token, "EMP-LIVE-ORG2", org_id=2)

    resp1 = client.get("/attendance/live_now", headers=h, params={"org_id": 1})
    ids1 = [e["employee_id"] for e in resp1.json()["employees"]]
    assert "EMP-LIVE-ORG1" in ids1
    assert "EMP-LIVE-ORG2" not in ids1

    resp2 = client.get("/attendance/live_now", headers=h, params={"org_id": 2})
    ids2 = [e["employee_id"] for e in resp2.json()["employees"]]
    assert "EMP-LIVE-ORG2" in ids2
    assert "EMP-LIVE-ORG1" not in ids2
