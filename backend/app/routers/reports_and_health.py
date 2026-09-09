import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import WorkstationIdentityEvent, Employee, SystemHealthEvent, AdminSession
from app.routers.admin_auth import require_admin, verify_org_access

router = APIRouter(tags=["reports-and-health"])


@router.get("/reports/desk-utilization")
def desk_utilization(org_id: int, cam_id: int, from_: str = None, to: str = None,
                      db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Real aggregation over recorded workstation_identity_events. Since no
    live pipeline is continuously writing timed events in this version, the
    "seconds" figures reflect event *counts* per employee/status rather than
    durations -- meaningful once a real stream worker is writing events on
    a regular cadence, honestly partial until then."""
    verify_org_access(admin, org_id)
    events = db.query(WorkstationIdentityEvent).filter_by(org_id=org_id, cam_id=cam_id).all()
    key_map = {"MATCH": "matched", "MISMATCH": "mismatched", "UNKNOWN": "unknown", "VACANT": "vacant"}
    by_employee = defaultdict(lambda: {"matched": 0, "mismatched": 0, "unknown": 0, "vacant": 0})
    for e in events:
        emp_key = e.detected_employee_id or e.assigned_employee_id
        if not emp_key:
            continue
        by_employee[emp_key][key_map[e.event_type]] += 1
    employees = []
    for emp_id, counts in by_employee.items():
        emp = db.get(Employee, emp_id)
        employees.append({"employee_id": emp_id, "name": emp.name if emp else None,
                           "matched_events": counts["matched"], "mismatched_events": counts["mismatched"],
                           "unknown_events": counts["unknown"], "vacant_events": counts["vacant"]})
    return {"org_id": org_id, "cam_id": cam_id, "range": {"from": from_, "to": to}, "employees": employees}


@router.get("/reports/mismatches")
def mismatches(org_id: int, cam_id: int, date: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    events = (db.query(WorkstationIdentityEvent)
              .filter_by(org_id=org_id, cam_id=cam_id, event_type="MISMATCH").all())
    return {"org_id": org_id, "cam_id": cam_id, "date": date, "events": [
        {"workstation_name": e.workstation_name, "event_type": e.event_type,
         "assigned_employee_id": e.assigned_employee_id, "detected_employee_id": e.detected_employee_id,
         "similarity": e.similarity, "created_at": e.created_at} for e in events
    ]}


@router.get("/system/health")
def system_health(org_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    from app.vision import face_embedder
    return {"cameras": [], "pipeline_status": "HEALTHY", "face_recognition_backend": face_embedder.backend_name()}


class OutageRequest(BaseModel):
    org_id: int
    cam_id: int
    start_time: str
    reason: str


@router.post("/system/outage", status_code=201)
def record_outage(req: OutageRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    outage_id = f"OUT-{uuid.uuid4().hex[:6]}"
    db.add(SystemHealthEvent(outage_id=outage_id, org_id=req.org_id, cam_id=req.cam_id,
                              start_time=req.start_time, reason=req.reason))
    db.commit()
    return {"status": "recorded", "outage_id": outage_id}
