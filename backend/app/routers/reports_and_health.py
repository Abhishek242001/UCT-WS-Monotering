import os
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (WorkstationIdentityEvent, Employee, SystemHealthEvent, AdminSession,
                         EmployeeFaceGallery, Workstation, WorkstationAssignment)
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
    """Previously returned a hardcoded pipeline_status: HEALTHY with no
    real checks behind it. Now actually probes the things that silently
    determine whether video/RTSP features will work on THIS server --
    each of these differs by deployment (confirmed different between this
    project's dev sandbox and prior findings), so guessing from another
    environment isn't reliable; this endpoint lets you check the actual
    server instead."""
    verify_org_access(admin, org_id)
    from app.vision import face_embedder

    import shutil
    ffmpeg_available = shutil.which("ffmpeg") is not None

    import cv2
    build_info = cv2.getBuildInformation()
    opencv_ffmpeg_support = "FFMPEG:                      YES" in build_info
    # RTSP itself is demuxed by OpenCV's FFMPEG backend (see
    # stream_worker.py's module docstring) -- if this is NO, RTSP camera
    # sources will fail to open regardless of anything else being correct.

    mp4v_writer_ok = False
    try:
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        # The OS temp dir, not a hard-coded /tmp (which does not exist on Windows).
        import tempfile
        health_path = os.path.join(tempfile.gettempdir(), "_health_check_mp4v.mp4")
        test_writer = cv2.VideoWriter(health_path, fourcc, 20.0, (64, 64))
        mp4v_writer_ok = test_writer.isOpened()
        test_writer.release()
        if os.path.exists(health_path):
            os.remove(health_path)
    except Exception:
        pass

    yolo_loadable = True
    yolo_error = None
    try:
        from app.vision import yolo_detector
        yolo_detector.get_model()  # cached after first call -- cheap on repeat health checks
    except Exception as e:
        yolo_loadable = False
        yolo_error = str(e)

    return {
        "pipeline_status": "HEALTHY" if (yolo_loadable and opencv_ffmpeg_support) else "DEGRADED",
        "face_recognition_backend": face_embedder.backend_name(),
        "face_recognition_is_stub": face_embedder.backend_name() == "stub",
        "face_recognition_load_error": face_embedder.load_error(),
        "yolo_model_loadable": yolo_loadable,
        "yolo_load_error": yolo_error,
        "opencv_ffmpeg_support": opencv_ffmpeg_support,
        "rtsp_capable": opencv_ffmpeg_support,  # same underlying dependency -- see comment above
        "ffmpeg_cli_available": ffmpeg_available,
        "annotated_video_playable_in_browser": ffmpeg_available,  # video_export.py re-encodes via this CLI
        "mp4v_video_writer_available": mp4v_writer_ok,
        "cameras": [],
    }


REQUIRED_VIEWS = ("front", "left", "right", "top")


@router.get("/reports/face-recognition-readiness")
def face_recognition_readiness(org_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Answers "is all the information actually present for face
    recognition to work" directly, rather than requiring someone to
    manually cross-check the Employees and Workstations tabs. Checks,
    per active employee: which of the 4 required views are enrolled, and
    whether each enrolled view actually has a non-null embedding (a
    gallery row can exist with embedding=NULL if extraction failed but
    the row was still created -- checked for real here, not assumed
    impossible). Checks, per saved workstation ROI: whether it currently
    has an active employee assignment (effective_to IS NULL) -- an
    unassigned workstation is a real gap: identify() will still run and
    can return MISMATCH for a textbook-correct detection, not something
    more obviously labeled "nobody assigned here", since
    is_assigned_employee compares against None and is always False.

    Does NOT report calibration/training status: the fitted near_k/
    near_sigma0 curve is currently NOT consumed anywhere in the live
    detection path (stream_worker.py, video_export.py, and
    workstations.py's simulate_detection all use a fixed
    snr = similarity * 10 placeholder instead) -- surfacing a "trained: yes"
    field here would misleadingly imply it affects live matching today,
    when it doesn't.
    """
    verify_org_access(admin, org_id)

    employees = db.query(Employee).filter_by(org_id=org_id, active=True).all()
    employee_readiness = []
    for emp in employees:
        gallery_rows = db.query(EmployeeFaceGallery).filter_by(employee_id=emp.employee_id).all()
        views_present = {row.view for row in gallery_rows}
        views_with_null_embedding = [row.view for row in gallery_rows if not row.embedding]
        views_missing = [v for v in REQUIRED_VIEWS if v not in views_present]
        employee_readiness.append({
            "employee_id": emp.employee_id, "name": emp.name,
            "views_enrolled": sorted(views_present), "views_missing": views_missing,
            "views_with_null_embedding": views_with_null_embedding,
            "complete": not views_missing and not views_with_null_embedding,
        })

    workstations = db.query(Workstation).filter_by(org_id=org_id).all()
    workstation_readiness = []
    for ws in workstations:
        active_assignment = (
            db.query(WorkstationAssignment)
            .filter_by(org_id=org_id, cam_id=ws.cam_id, workstation_name=ws.name, effective_to=None)
            .order_by(WorkstationAssignment.effective_from.desc()).first()
        )
        workstation_readiness.append({
            "cam_id": ws.cam_id, "name": ws.name,
            "assigned_employee_id": active_assignment.employee_id if active_assignment else None,
            "has_active_assignment": active_assignment is not None,
        })

    return {
        "org_id": org_id,
        "employees": employee_readiness,
        "employees_complete_count": sum(1 for e in employee_readiness if e["complete"]),
        "employees_total_count": len(employee_readiness),
        "workstations": workstation_readiness,
        "workstations_unassigned_count": sum(1 for w in workstation_readiness if not w["has_active_assignment"]),
        "note": "calibration/training status intentionally not reported here -- "
                "see this endpoint's docstring, near_k/near_sigma0 are not "
                "currently used by live detection regardless of training status.",
    }


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
