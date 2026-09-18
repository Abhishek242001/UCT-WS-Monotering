import os
import shutil
import tempfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Employee, EmployeeFaceGallery
from app.vision import face_embedder
from app.routers.admin_auth import require_admin, verify_org_access
from app.models import AdminSession

router = APIRouter(tags=["employees"])

VALID_VIEWS = {"front", "left", "right", "top"}
def _upload_dir() -> str:
    """Read at call time, not import time -- see dataset_calibration.py's
    _data_dir() for why."""
    return os.environ.get("ENROLLMENT_PHOTO_DIR", "./enrollment_photos")


@router.post("/employees/enroll", status_code=201)
async def enroll_employee(
    org_id: int = Form(...), employee_id: str = Form(...), name: str = Form(...),
    view: str = Form(...), depth_m: float = Form(1.0), photo: UploadFile = File(...),
    db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin),
):
    """Enrolls one view for an employee. Call once per view (front/left/
    right/top) with a real photo -- matches the dataset structure documented
    in Section 5.2, just submitted one capture at a time via a normal file
    upload instead of a pre-built zip folder tree."""
    verify_org_access(admin, org_id)
    if view not in VALID_VIEWS:
        raise HTTPException(422, f"view must be one of {VALID_VIEWS}")

    employee = db.get(Employee, employee_id)
    if not employee:
        employee = Employee(employee_id=employee_id, org_id=org_id, name=name, active=True)
        db.add(employee)
        db.flush()
    elif employee.org_id != org_id:
        # employee_id is a globally unique primary key, so without this
        # check a caller could silently attach gallery photos to another
        # organization's existing employee record just by reusing their
        # employee_id -- a real cross-tenant data leakage path, not a
        # hypothetical one, since nothing else about this request proves
        # the caller has any relationship to that employee's actual org.
        raise HTTPException(409, f"employee_id '{employee_id}' already exists under a different org_id")

    os.makedirs(_upload_dir(), exist_ok=True)
    dest_path = os.path.join(_upload_dir(), f"{employee_id}__{view}.jpg")
    with open(dest_path, "wb") as f:
        shutil.copyfileobj(photo.file, f)

    try:
        embedding, width_px = face_embedder.extract_embedding(dest_path)
    except ValueError as e:
        raise HTTPException(422, str(e))

    existing = db.query(EmployeeFaceGallery).filter_by(employee_id=employee_id, view=view).first()
    if existing:
        existing.set_embedding(embedding)
        existing.reference_width_px = width_px
        existing.reference_depth_m = depth_m
    else:
        row = EmployeeFaceGallery(employee_id=employee_id, view=view, reference_depth_m=depth_m,
                                   reference_width_px=width_px)
        row.set_embedding(embedding)
        db.add(row)

    db.commit()
    views_captured = [r.view for r in db.query(EmployeeFaceGallery).filter_by(employee_id=employee_id).all()]
    return {
        "status": "enrolled", "employee_id": employee_id, "views_captured": sorted(views_captured),
        "calibration_pending": len(views_captured) < 4, "face_backend": face_embedder.backend_name(),
    }


@router.get("/employees/list")
def list_employees(org_id: int, include_inactive: bool = False, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Defaults to active employees only -- an admin who just deleted
    someone expects them gone from the normal list, not merely flagged.
    Pass include_inactive=true for an HR-audit view that also shows
    offboarded employees (their historical records are preserved, not
    erased -- see delete_employee's docstring)."""
    verify_org_access(admin, org_id)
    query = db.query(Employee).filter_by(org_id=org_id)
    if not include_inactive:
        query = query.filter_by(active=True)
    rows = query.all()
    return {"employees": [{"employee_id": e.employee_id, "name": e.name, "department": e.department,
                            "active": e.active, "created_at": e.created_at} for e in rows]}


from pydantic import BaseModel  # noqa: E402


class EmployeeDeleteBody(BaseModel):
    org_id: int
    employee_id: str


@router.delete("/employees/delete")
def delete_employee(req: EmployeeDeleteBody, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Soft-deletes the employee (active=False) while hard-deleting only
    their actual biometric data (the face gallery) -- NOT a full row
    delete. This was a real bug, not just a documentation mismatch: only
    EmployeeFaceGallery cascades on employee_id; workstation_assignments,
    employee_attendance, attendance_segments, employee_shift_assignment,
    and attendance_exceptions do not. A hard delete here previously threw
    a foreign-key IntegrityError for any employee with real history (an
    assignment, a single attendance record, anything) -- found via an
    end-to-end dry run using a realistic sequence of operations, not an
    isolated unit test with a freshly-created employee and no history.

    Soft-deleting also matches the documented design intent (see
    Employee.active in models.py and Section 7.2 of the project
    documentation: "soft-delete flag... preserving history after
    offboarding"), and is arguably the more correct behavior for an HR
    system regardless of the bug -- you don't want an ex-employee's
    attendance record to vanish, only their biometric data, which is the
    actual privacy-sensitive part this endpoint's "right to be forgotten"
    purpose is about."""
    verify_org_access(admin, req.org_id)
    employee = db.get(Employee, req.employee_id)
    if not employee or employee.org_id != req.org_id:
        # Deliberately the same 404 for "doesn't exist" and "exists under a
        # different org" -- returning a different error for the latter
        # would itself leak that the employee_id exists somewhere, just
        # not in the caller's org.
        raise HTTPException(404, "Employee not found")
    gallery_rows = db.query(EmployeeFaceGallery).filter_by(employee_id=req.employee_id).all()
    removed = len(gallery_rows)
    for row in gallery_rows:
        db.delete(row)
    employee.active = False
    db.commit()
    return {"status": "deleted", "employee_id": req.employee_id, "gallery_entries_removed": removed}
