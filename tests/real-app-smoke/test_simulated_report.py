"""
Verifies /attendance/simulated/report -- completes simulated-data parity
with the real attendance endpoints (today and segments already had
simulated counterparts from item 8; report didn't yet).
"""
import sys
from datetime import datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
SAMPLE_PHOTO = str(BACKEND_DIR.parent / "sample_data" / "test-photo.jpg")


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_simulated_report_reflects_simulated_data_not_real(client, admin_token):
    from app.database import SessionLocal
    from app.routers.attendance import record_simulated_detection_core, record_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SIM-REPORT", "name": "Sim Report Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    db = SessionLocal()
    record_simulated_detection_core(db, 1, "EMP-SIM-REPORT", cam_id=90001, location="DemoDesk",
                                     timestamp=datetime(2026, 3, 1, 9, 0, 0))
    record_detection_core(db, 1, "EMP-SIM-REPORT", cam_id=100, location="RealDesk",
                           timestamp=datetime(2026, 3, 2, 9, 0, 0))
    db.close()

    sim_resp = client.get("/attendance/simulated/report", headers=h,
                           params={"org_id": 1, "employee_id": "EMP-SIM-REPORT"})
    sim_dates = [d["date"] for d in sim_resp.json()["days"]]
    assert "2026-03-01" in sim_dates
    assert "2026-03-02" not in sim_dates, "the REAL detection's date must not leak into the simulated report"

    real_resp = client.get("/attendance/report", headers=h,
                            params={"org_id": 1, "employee_id": "EMP-SIM-REPORT"})
    real_dates = [d["date"] for d in real_resp.json()["days"]]
    assert "2026-03-02" in real_dates
    assert "2026-03-01" not in real_dates, "the SIMULATED detection's date must not leak into the real report"


def test_simulated_report_respects_date_range(client, admin_token):
    from app.database import SessionLocal
    from app.routers.attendance import record_simulated_detection_core

    h = auth(admin_token)
    client.post("/employees/enroll", headers=h, data={
        "org_id": 1, "employee_id": "EMP-SIM-REPORT-2", "name": "Range Test", "view": "front", "depth_m": 1.0,
    }, files={"photo": ("f.jpg", open(SAMPLE_PHOTO, "rb"), "image/jpeg")})

    db = SessionLocal()
    for day in (1, 5, 10):
        record_simulated_detection_core(db, 1, "EMP-SIM-REPORT-2", cam_id=90001, location="DemoDesk",
                                         timestamp=datetime(2026, 4, day, 9, 0, 0))
    db.close()

    resp = client.get("/attendance/simulated/report", headers=h, params={
        "org_id": 1, "employee_id": "EMP-SIM-REPORT-2", "from_": "2026-04-03", "to": "2026-04-10",
    })
    dates = [d["date"] for d in resp.json()["days"]]
    assert dates == ["2026-04-05", "2026-04-10"]
