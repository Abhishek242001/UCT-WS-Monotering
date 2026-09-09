from datetime import datetime, timedelta, time as dtime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    EmployeeAttendance, AttendanceSegment, AttendanceException, Employee,
    EmployeeShiftAssignment, BreakSchedule, AdminSession,
)
from app import logic
from app.routers.admin_auth import require_admin, verify_org_access

router = APIRouter(tags=["attendance"])

GRACE_PERIOD_SECONDS = int(__import__("os").environ.get("ATTENDANCE_GRACE_PERIOD_SECONDS", 600))
AWAY_TIMEOUT_SECONDS = int(__import__("os").environ.get("ATTENDANCE_AWAY_TIMEOUT_SECONDS", 3600))


def _parse_hhmm(value: str) -> dtime:
    parts = [int(p) for p in value.split(":")]
    return dtime(*parts[:2])


def _current_break_windows(db: Session, org_id: int, employee_id: str, on_date: str) -> list[logic.BreakWindow]:
    """Real lookup, previously missing: finds the employee's currently
    effective shift assignment as of on_date, then that shift's configured
    break windows. Without this, next_attendance_status() was always
    called with break_windows=[], meaning ON_BREAK could never actually be
    reached through this endpoint even though shifts/break_schedules had
    working CRUD and the state-machine logic itself was fully correct and
    tested -- a real gap between what the schema/logic supported and what
    was actually wired up."""
    assignment = (
        db.query(EmployeeShiftAssignment)
        .filter(
            EmployeeShiftAssignment.org_id == org_id,
            EmployeeShiftAssignment.employee_id == employee_id,
            EmployeeShiftAssignment.effective_from <= on_date,
        )
        .filter(
            (EmployeeShiftAssignment.effective_to.is_(None)) | (EmployeeShiftAssignment.effective_to >= on_date)
        )
        .order_by(EmployeeShiftAssignment.effective_from.desc())
        .first()
    )
    if not assignment:
        return []

    breaks = db.query(BreakSchedule).filter_by(org_id=org_id, shift_id=assignment.shift_id).all()
    return [logic.BreakWindow(start=_parse_hhmm(b.start_time), end=_parse_hhmm(b.end_time)) for b in breaks]


class RecordDetectionRequest(BaseModel):
    org_id: int
    employee_id: str
    cam_id: int | None = None
    location: str | None = None
    timestamp: str | None = None  # ISO datetime; defaults to now


@router.post("/attendance/record_detection", status_code=201)
def record_detection(req: RecordDetectionRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Not one of the 35 documented public endpoints -- this is the real
    ingestion point that would normally be called by the stream-processing
    worker described in Section 3.2 whenever an employee is identity-
    confirmed at any camera. Exposed directly here so sign-in/out can
    actually be exercised without a live video pipeline."""
    verify_org_access(admin, req.org_id)
    employee = db.get(Employee, req.employee_id)
    if not employee or employee.org_id != req.org_id:
        # Without this check, a caller could write an EmployeeAttendance
        # row scoped to org_id=2 that actually references an employee who
        # belongs to org 1 -- corrupting data integrity (an attendance
        # record pointing at someone outside its own org's employee list)
        # and letting org 2 fabricate presence data for a person who was
        # never actually theirs to track.
        raise HTTPException(404, "Employee not found")

    ts = datetime.fromisoformat(req.timestamp) if req.timestamp else datetime.utcnow()
    date_str = ts.date().isoformat()

    rec = db.query(EmployeeAttendance).filter_by(org_id=req.org_id, employee_id=req.employee_id, date=date_str).first()
    if not rec:
        rec = EmployeeAttendance(org_id=req.org_id, employee_id=req.employee_id, date=date_str,
                                  sign_in_time=ts.time().isoformat(timespec="seconds"), status="PRESENT")
        db.add(rec)
    else:
        gap_seconds = (ts - datetime.fromisoformat(f"{date_str}T{rec.last_seen_at.split('T')[-1]}")).total_seconds() \
            if rec.last_seen_at else 0
        current_status = logic.AttendanceStatus(rec.status)
        break_windows = _current_break_windows(db, req.org_id, req.employee_id, date_str)
        new_status = logic.next_attendance_status(
            current_status=current_status, detected_now=True, current_time=ts.time(),
            break_windows=break_windows, gap_seconds=max(gap_seconds, 0),
            grace_period_seconds=GRACE_PERIOD_SECONDS, away_timeout_seconds=AWAY_TIMEOUT_SECONDS,
        )
        rec.status = new_status.value
        if rec.sign_out_time:
            rec.sign_out_time = None  # reappeared same day -- clear any prior close-out

    rec.last_seen_at = ts.isoformat()
    rec.last_seen_cam_id = req.cam_id
    rec.last_seen_workstation = req.location
    db.commit()

    if req.location:
        db.add(AttendanceSegment(org_id=req.org_id, employee_id=req.employee_id, date=date_str,
                                  cam_id=req.cam_id, department_or_workstation=req.location,
                                  start_time=ts.time().isoformat(timespec="seconds")))
        db.commit()

    return {"org_id": rec.org_id, "employee_id": rec.employee_id, "date": rec.date,
            "sign_in_time": rec.sign_in_time, "status": rec.status, "last_seen_at": rec.last_seen_at}


@router.get("/attendance/today")
def attendance_today(org_id: int, employee_id: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    today = datetime.utcnow().date().isoformat()
    rec = db.query(EmployeeAttendance).filter_by(org_id=org_id, employee_id=employee_id, date=today).first()
    if not rec:
        raise HTTPException(404, "No attendance record for today")
    return {"org_id": rec.org_id, "employee_id": rec.employee_id, "date": rec.date,
            "sign_in_time": rec.sign_in_time, "sign_out_time": rec.sign_out_time, "status": rec.status,
            "last_seen_at": rec.last_seen_at, "last_seen_workstation": rec.last_seen_workstation}


@router.get("/attendance/report")
def attendance_report(org_id: int, employee_id: str, from_: str | None = None, to: str | None = None,
                       db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    q = db.query(EmployeeAttendance).filter_by(org_id=org_id, employee_id=employee_id)
    if from_:
        q = q.filter(EmployeeAttendance.date >= from_)
    if to:
        q = q.filter(EmployeeAttendance.date <= to)
    rows = q.order_by(EmployeeAttendance.date).all()
    return {"employee_id": employee_id, "days": [
        {"date": r.date, "sign_in_time": r.sign_in_time, "sign_out_time": r.sign_out_time,
         "net_present_seconds": r.net_present_seconds, "day_classification": r.day_classification,
         "status": r.status} for r in rows
    ]}


@router.get("/attendance/segments")
def attendance_segments(org_id: int, employee_id: str, date: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    rows = db.query(AttendanceSegment).filter_by(org_id=org_id, employee_id=employee_id, date=date).all()
    return {"employee_id": employee_id, "date": date, "segments": [
        {"cam_id": r.cam_id, "location": r.department_or_workstation, "start_time": r.start_time,
         "end_time": r.end_time, "duration_seconds": r.duration_seconds} for r in rows
    ]}


class ExceptionRequest(BaseModel):
    org_id: int
    employee_id: str
    date: str
    type: str
    reason: str
    admin_user: str


VALID_EXCEPTION_TYPES = {"FIELD_WORK", "LEAVE", "WFH", "CORRECTION"}


@router.post("/attendance/exception", status_code=201)
def record_exception(req: ExceptionRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    if req.type not in VALID_EXCEPTION_TYPES:
        raise HTTPException(422, f"type must be one of {VALID_EXCEPTION_TYPES}")
    employee = db.get(Employee, req.employee_id)
    if not employee or employee.org_id != req.org_id:
        # This endpoint previously had no employee-existence check at all,
        # let alone an org_id check -- it would happily record an
        # exception against an employee_id that didn't exist anywhere, or
        # that belonged to a different org entirely.
        raise HTTPException(404, "Employee not found")
    import uuid
    exception_id = f"EXC-{uuid.uuid4().hex[:8]}"
    db.add(AttendanceException(exception_id=exception_id, org_id=req.org_id, employee_id=req.employee_id,
                                date=req.date, type=req.type, reason=req.reason, admin_user=req.admin_user))
    db.commit()
    return {"status": "recorded", "exception_id": exception_id}


@router.post("/attendance/close_stale_sessions")
def close_stale_sessions(db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Maintenance endpoint: applies the away-timeout rule (Section 6.1) to
    every PRESENT/ON_BREAK record whose last_seen_at has exceeded the
    configured timeout, and computes net_present_seconds + day
    classification on close-out. Intended to run periodically (e.g. via
    cron); exposed as a callable endpoint here since no scheduler is wired
    up in this initial version.

    Restricted to SUPER_ADMIN: this sweeps every org's records in one
    pass, which is exactly the kind of genuinely cross-tenant operation
    verify_org_access() can't (and shouldn't) authorize an ordinary
    org-scoped HR_ADMIN for."""
    if admin.role != "SUPER_ADMIN":
        raise HTTPException(403, "Only a SUPER_ADMIN account can run this system-wide maintenance operation")
    now = datetime.utcnow()
    closed = []
    rows = db.query(EmployeeAttendance).filter(EmployeeAttendance.status != "SIGNED_OUT").all()
    for rec in rows:
        if not rec.last_seen_at:
            continue
        last_seen = datetime.fromisoformat(rec.last_seen_at)
        if logic.should_mark_signed_out(last_seen, now, AWAY_TIMEOUT_SECONDS):
            rec.status = "SIGNED_OUT"
            rec.sign_out_time = last_seen.time().isoformat(timespec="seconds")
            sign_in_dt = datetime.fromisoformat(f"{rec.date}T{rec.sign_in_time}")
            rec.net_present_seconds = logic.compute_net_present_seconds(sign_in_dt, last_seen, break_seconds=0)
            rec.day_classification = logic.classify_day(
                rec.net_present_seconds, full_day_threshold_seconds=7 * 3600, half_day_threshold_seconds=4 * 3600
            ).value
            closed.append(rec.employee_id)
    db.commit()
    return {"closed_count": len(closed), "employee_ids": closed}
