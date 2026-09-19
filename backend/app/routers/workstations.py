import os
import shutil
import tempfile

import cv2
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Workstation, WorkstationAssignment, Employee, EmployeeFaceGallery, WorkstationIdentityEvent
from app import logic
from app.vision import face_crop, yolo_detector, face_embedder
from app.routers.admin_auth import require_admin, verify_org_access
from app.models import AdminSession

router = APIRouter(tags=["workstations"])


class WorkstationDef(BaseModel):
    name: str
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)
    x2: float = Field(ge=0.0, le=1.0)
    y2: float = Field(ge=0.0, le=1.0)


class WorkstationsSaveRequest(BaseModel):
    org_id: int
    cam_id: int
    workstations: list[WorkstationDef]


@router.post("/workstations/save")
def save_workstations(req: WorkstationsSaveRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    for ws in req.workstations:
        existing = db.query(Workstation).filter_by(org_id=req.org_id, cam_id=req.cam_id, name=ws.name).first()
        if existing:
            existing.x1, existing.y1, existing.x2, existing.y2 = ws.x1, ws.y1, ws.x2, ws.y2
        else:
            db.add(Workstation(org_id=req.org_id, cam_id=req.cam_id, name=ws.name,
                                x1=ws.x1, y1=ws.y1, x2=ws.x2, y2=ws.y2))
    db.commit()
    return {"status": "saved", "org_id": req.org_id, "cam_id": req.cam_id, "count": len(req.workstations)}


@router.get("/workstations/check")
def check_workstations(org_id: int, cam_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    rows = db.query(Workstation).filter_by(org_id=org_id, cam_id=cam_id).all()
    return {
        "has_workstations": len(rows) > 0,
        "count": len(rows),
        "workstations": [{"name": r.name, "x1": r.x1, "y1": r.y1, "x2": r.x2, "y2": r.y2} for r in rows],
    }


class WorkstationDeleteRequest(BaseModel):
    org_id: int
    cam_id: int
    name: Optional[str] = None


@router.delete("/workstations/delete")
def delete_workstations(req: WorkstationDeleteRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    q = db.query(Workstation).filter_by(org_id=req.org_id, cam_id=req.cam_id)
    if req.name:
        q = q.filter_by(name=req.name)
    rows = q.all()
    if not rows:
        raise HTTPException(404, "No matching workstations found")
    for r in rows:
        db.delete(r)
    db.commit()
    return {"status": "deleted", "org_id": req.org_id, "cam_id": req.cam_id, "deleted_count": len(rows)}


class AssignRequest(BaseModel):
    org_id: int
    cam_id: int
    workstation_name: str
    employee_id: str
    effective_from: str


@router.post("/workstations/assign")
def assign_workstation(req: AssignRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    employee = db.get(Employee, req.employee_id)
    if not employee or employee.org_id != req.org_id:
        # Without the org_id check, org B could assign org A's employee to
        # one of org B's own workstations just by knowing/guessing their
        # employee_id -- employee_id is a global primary key, so existence
        # alone proves nothing about which org actually owns that person.
        raise HTTPException(404, "Employee not found")
    ws = db.query(Workstation).filter_by(org_id=req.org_id, cam_id=req.cam_id, name=req.workstation_name).first()
    if not ws:
        raise HTTPException(404, "Workstation not found")
    db.add(WorkstationAssignment(org_id=req.org_id, cam_id=req.cam_id, workstation_name=req.workstation_name,
                                  employee_id=req.employee_id, effective_from=req.effective_from))
    db.commit()
    return {"status": "assigned", "org_id": req.org_id, "cam_id": req.cam_id,
            "workstation_name": req.workstation_name, "employee_id": req.employee_id}


def _current_assignment(db: Session, org_id: int, cam_id: int, name: str) -> Optional[str]:
    row = (db.query(WorkstationAssignment)
           .filter_by(org_id=org_id, cam_id=cam_id, workstation_name=name, effective_to=None)
           .order_by(WorkstationAssignment.effective_from.desc()).first())
    return row.employee_id if row else None


def _latest_event(db: Session, org_id: int, cam_id: int, name: str) -> Optional[WorkstationIdentityEvent]:
    return (db.query(WorkstationIdentityEvent)
            .filter_by(org_id=org_id, cam_id=cam_id, workstation_name=name)
            .order_by(WorkstationIdentityEvent.id.desc()).first())


@router.get("/workstations/identity_status")
def identity_status(org_id: int, cam_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    workstations = db.query(Workstation).filter_by(org_id=org_id, cam_id=cam_id).all()
    out = []
    for ws in workstations:
        assigned = _current_assignment(db, org_id, cam_id, ws.name)
        latest = _latest_event(db, org_id, cam_id, ws.name)
        if latest:
            out.append({
                "name": ws.name, "occupancy_status": "VACANT" if latest.event_type == "VACANT" else "ACTIVE",
                "assigned_employee_id": assigned, "detected_employee_id": latest.detected_employee_id,
                "match_status": latest.event_type, "similarity": latest.similarity, "snr": latest.snr,
                "distance_m": latest.distance_m,
            })
        else:
            out.append({"name": ws.name, "occupancy_status": "VACANT", "assigned_employee_id": assigned,
                         "detected_employee_id": None, "match_status": "VACANT",
                         "similarity": None, "snr": None, "distance_m": None})
    return {"org_id": org_id, "cam_id": cam_id, "workstations": out}


# ---------------------------------------------------------------------------
# "Try it" endpoint (not part of the documented 35-endpoint spec): upload an
# actual photo/frame and see the real YOLO + face-matching pipeline run,
# end to end, against your enrolled employees and saved ROI. This is what
# ties app/vision/yolo_detector.py and app/vision/face_embedder.py together
# into something you can actually exercise without a live camera feed.
# ---------------------------------------------------------------------------
@router.post("/workstations/simulate_detection")
async def simulate_detection(
    org_id: int = Form(...), cam_id: int = Form(...), workstation_name: str = Form(...),
    frame: UploadFile = File(...), db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    verify_org_access(admin, org_id)
    ws = db.query(Workstation).filter_by(org_id=org_id, cam_id=cam_id, name=workstation_name).first()
    if not ws:
        raise HTTPException(404, "Workstation not found")

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(frame.filename or "frame.jpg")[1] or ".jpg", delete=False) as tmp:
        shutil.copyfileobj(frame.file, tmp)
        tmp_path = tmp.name

    try:
        people = yolo_detector.detect_people(tmp_path)
        roi = {"x1": ws.x1, "y1": ws.y1, "x2": ws.x2, "y2": ws.y2}
        occupying_person = face_crop.best_overlapping_person(people, roi)
        occupied = occupying_person is not None

        assigned_employee_id = _current_assignment(db, org_id, cam_id, workstation_name)
        event_type, detected_employee_id, similarity, snr = "VACANT", None, None, None

        if occupied:
            crop_path = face_crop.crop_person_region(cv2.imread(tmp_path), occupying_person)
            try:
                live_embedding, live_width_px = face_embedder.extract_embedding(crop_path)
                gallery_rows = db.query(EmployeeFaceGallery).join(Employee).filter(Employee.org_id == org_id).all()
                gallery = {f"{row.employee_id}__{row.view}": row.get_embedding() for row in gallery_rows if row.embedding}
                grouped = logic.group_gallery_by_person(gallery)
                best = logic.match_single_pass(live_embedding, grouped)

                if best is None:
                    event_type = "UNKNOWN"
                else:
                    detected_employee_id, _view, similarity = best
                    snr = similarity * 10  # placeholder scale without a fitted per-employee decay curve yet
                    status = logic.decide_match_status(
                        occupancy_present=True, best_similarity=similarity, best_snr=snr,
                        is_assigned_employee=(detected_employee_id == assigned_employee_id),
                    )
                    event_type = status.value
                    if event_type == "UNKNOWN":
                        detected_employee_id = None
            except ValueError:
                event_type = "UNKNOWN"  # occupied per YOLO, but no face found/matched in the cropped region
            finally:
                os.unlink(crop_path)

        event = WorkstationIdentityEvent(
            org_id=org_id, cam_id=cam_id, workstation_name=workstation_name, event_type=event_type,
            assigned_employee_id=assigned_employee_id, detected_employee_id=detected_employee_id,
            similarity=similarity, snr=snr,
        )
        db.add(event)
        db.commit()

        return {
            "workstation_name": workstation_name, "people_detected": len(people), "occupied": occupied,
            "event_type": event_type, "assigned_employee_id": assigned_employee_id,
            "detected_employee_id": detected_employee_id, "similarity": similarity, "snr": snr,
            "face_backend": face_embedder.backend_name(),
        }
    finally:
        os.unlink(tmp_path)
