from datetime import datetime, timedelta, time as dtime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    EmployeeAttendance, AttendanceSegment, SimulatedAttendance, SimulatedAttendanceSegment,
    AttendanceException, Employee, EmployeeShiftAssignment, BreakSchedule, AdminSession,
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
    was actually wired up. Shared by both the real and simulated paths --
    shift/break config isn't something Video Analysis needs its own copy
    of, only the resulting attendance/segment DATA is kept separate."""
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


def _close_open_segment_and_open_new(db: Session, SegmentModel, org_id: int, employee_id: str, date_str: str,
                                      cam_id: int | None, location: str, ts: datetime) -> None:
    """Closes the currently-open segment (if any) before opening a new
    one -- unless the person is still at the SAME location, in which case
    the open segment just continues (nothing to do).

    Previously, every single call opened a brand-new segment
    unconditionally, even when the location hadn't changed -- for a
    person checked every 60 seconds at the same desk, that meant a new
    micro-segment every minute instead of one continuous one, and
    end_time/duration_seconds were never set anywhere in the codebase at
    all: the schema supported per-segment duration, but nothing ever
    computed it. Fixed by only opening a new segment on a genuine
    location change, and computing the closed segment's real duration
    from its own recorded start_time to this new detection's timestamp.

    SegmentModel is either AttendanceSegment (real) or
    SimulatedAttendanceSegment (Video Analysis) -- see
    models.SimulatedAttendance's docstring for why these are separate
    tables rather than one table with a discriminator column.
    """
    open_segment = (
        db.query(SegmentModel)
        .filter_by(org_id=org_id, employee_id=employee_id, date=date_str, end_time=None)
        .order_by(SegmentModel.id.desc())
        .first()
    )
    if open_segment and open_segment.department_or_workstation == location:
        return  # still at the same place -- the open segment just continues

    if open_segment:
        start_dt = datetime.fromisoformat(f"{date_str}T{open_segment.start_time}")
        open_segment.end_time = ts.time().isoformat(timespec="seconds")
        open_segment.duration_seconds = max((ts - start_dt).total_seconds(), 0)

    db.add(SegmentModel(org_id=org_id, employee_id=employee_id, date=date_str,
                         cam_id=cam_id, department_or_workstation=location,
                         start_time=ts.time().isoformat(timespec="seconds")))
    db.commit()


def _record_detection_impl(db: Session, AttendanceModel, SegmentModel, org_id: int, employee_id: str,
                            cam_id: int | None, location: str | None, timestamp: datetime | None):
    """The real attendance-recording logic, parameterized over which pair
    of tables to write to -- (EmployeeAttendance, AttendanceSegment) for a
    real live stream, or (SimulatedAttendance, SimulatedAttendanceSegment)
    for a Video Analysis run (item 8). Same state-machine logic either
    way; only the destination tables differ, which is exactly what keeps
    a demo run from ever being able to write into, or collide with, real
    attendance data -- they are different tables, not the same rows
    tagged differently.
    """
    employee = db.get(Employee, employee_id)
    if not employee or employee.org_id != org_id:
        return None

    ts = timestamp or datetime.utcnow()
    date_str = ts.date().isoformat()

    rec = db.query(AttendanceModel).filter_by(org_id=org_id, employee_id=employee_id, date=date_str).first()
    if not rec:
        rec = AttendanceModel(org_id=org_id, employee_id=employee_id, date=date_str,
                               sign_in_time=ts.time().isoformat(timespec="seconds"), status="PRESENT")
        db.add(rec)
    else:
        gap_seconds = (ts - datetime.fromisoformat(f"{date_str}T{rec.last_seen_at.split('T')[-1]}")).total_seconds() \
            if rec.last_seen_at else 0
        current_status = logic.AttendanceStatus(rec.status)
        break_windows = _current_break_windows(db, org_id, employee_id, date_str)
        new_status = logic.next_attendance_status(
            current_status=current_status, detected_now=True, current_time=ts.time(),
            break_windows=break_windows, gap_seconds=max(gap_seconds, 0),
            grace_period_seconds=GRACE_PERIOD_SECONDS, away_timeout_seconds=AWAY_TIMEOUT_SECONDS,
        )
        rec.status = new_status.value
        if rec.sign_out_time:
            rec.sign_out_time = None  # reappeared same day -- clear any prior close-out

    rec.last_seen_at = ts.isoformat()
    rec.last_seen_cam_id = cam_id
    rec.last_seen_workstation = location
    db.commit()

    if location:
        _close_open_segment_and_open_new(db, SegmentModel, org_id, employee_id, date_str, cam_id, location, ts)

    return rec


def record_detection_core(db: Session, org_id: int, employee_id: str, cam_id: int | None = None,
                           location: str | None = None, timestamp: datetime | None = None) -> EmployeeAttendance | None:
    """REAL attendance -- callable directly with a db session, extracted
    so app/vision/stream_worker.py can call this on every confirmed
    identification from a genuinely live stream (StreamWorker.is_live is
    True). Returns None (rather than raising) when the employee doesn't
    belong to org_id -- callers decide what that means for them (the
    HTTP endpoint below turns it into a 404; the stream worker just skips
    silently and logs)."""
    return _record_detection_impl(db, EmployeeAttendance, AttendanceSegment, org_id, employee_id, cam_id, location, timestamp)


def record_simulated_detection_core(db: Session, org_id: int, employee_id: str, cam_id: int | None = None,
                                     location: str | None = None, timestamp: datetime | None = None) -> SimulatedAttendance | None:
    """Item 8: Video Analysis' own attendance recording -- same logic as
    record_detection_core, but writes to SimulatedAttendance /
    SimulatedAttendanceSegment instead. Called by stream_worker.py when
    StreamWorker.is_live is False, so an uploaded demo video produces
    something viewable ("a glimpse of the software", per your own
    description of what Video Analysis is for) without ever touching a
    single row of real attendance data."""
    return _record_detection_impl(db, SimulatedAttendance, SimulatedAttendanceSegment, org_id, employee_id, cam_id, location, timestamp)


@router.post("/attendance/record_detection", status_code=201)
def record_detection(req: RecordDetectionRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Not one of the 35 documented public endpoints -- this is the real
    ingestion point that would normally be called by the stream-processing
    worker described in Section 3.2 whenever an employee is identity-
    confirmed at any camera (see record_detection_core, which
    stream_worker.py now calls directly). Exposed directly here too so
    sign-in/out can still be exercised without a live video pipeline.
    Always writes REAL attendance -- there is no is_live concept at the
    HTTP layer, only in the stream worker; a caller of this endpoint is
    always claiming a real detection happened."""
    verify_org_access(admin, req.org_id)
    ts = datetime.fromisoformat(req.timestamp) if req.timestamp else None
    rec = record_detection_core(db, req.org_id, req.employee_id, req.cam_id, req.location, ts)
    if rec is None:
        # Without this check, a caller could write an EmployeeAttendance
        # row scoped to org_id=2 that actually references an employee who
        # belongs to org 1 -- corrupting data integrity (an attendance
        # record pointing at someone outside its own org's employee list)
        # and letting org 2 fabricate presence data for a person who was
        # never actually theirs to track.
        raise HTTPException(404, "Employee not found")

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


@router.get("/attendance/live_now")
def attendance_live_now(org_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Item 10: "who's here right now" -- every existing attendance
    endpoint requires knowing an employee_id up front; there was no way
    to see the org's whole current presence at a glance. Returns every
    employee whose TODAY record is still PRESENT or ON_BREAK (i.e.
    genuinely here now, not SIGNED_OUT and not simply never seen today).
    Always reads REAL attendance -- "who's here" is inherently a live-
    monitoring question, not something a Video Analysis demo run answers."""
    verify_org_access(admin, org_id)
    today = datetime.utcnow().date().isoformat()
    rows = (
        db.query(EmployeeAttendance, Employee)
        .join(Employee, Employee.employee_id == EmployeeAttendance.employee_id)
        .filter(
            EmployeeAttendance.org_id == org_id,
            EmployeeAttendance.date == today,
            EmployeeAttendance.status.in_(["PRESENT", "ON_BREAK"]),
        )
        .all()
    )
    return {"org_id": org_id, "date": today, "employees": [
        {"employee_id": rec.employee_id, "name": emp.name, "status": rec.status,
         "last_seen_at": rec.last_seen_at, "last_seen_workstation": rec.last_seen_workstation}
        for rec, emp in rows
    ]}


@router.get("/attendance/simulated/today")
def simulated_attendance_today(org_id: int, employee_id: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Item 8: the Video Analysis counterpart to /attendance/today --
    reads SimulatedAttendance, never the real EmployeeAttendance table.
    "Today" here means the demo video's own detected timestamps, which
    may not correspond to when the video was actually uploaded/analyzed
    (a video can be analyzed at any real time; the simulated dates
    reflect whatever timestamps the frames were processed under)."""
    verify_org_access(admin, org_id)
    today = datetime.utcnow().date().isoformat()
    rec = db.query(SimulatedAttendance).filter_by(org_id=org_id, employee_id=employee_id, date=today).first()
    if not rec:
        raise HTTPException(404, "No simulated attendance record for today")
    return {"org_id": rec.org_id, "employee_id": rec.employee_id, "date": rec.date,
            "sign_in_time": rec.sign_in_time, "sign_out_time": rec.sign_out_time, "status": rec.status,
            "last_seen_at": rec.last_seen_at, "last_seen_workstation": rec.last_seen_workstation}


@router.get("/attendance/simulated/segments")
def simulated_attendance_segments(org_id: int, employee_id: str, date: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Item 8: the Video Analysis counterpart to /attendance/segments."""
    verify_org_access(admin, org_id)
    rows = db.query(SimulatedAttendanceSegment).filter_by(org_id=org_id, employee_id=employee_id, date=date).all()
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
    org-scoped HR_ADMIN for.

    Only sweeps REAL attendance (EmployeeAttendance) -- a Video Analysis
    run is a short, bounded, already-finished process by the time anyone
    would call this; there's no "stale" simulated session to close in the
    same away-timeout sense a real all-day live stream has."""
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

            # Close this employee's final open segment for the day too --
            # otherwise it stays open forever, since record_detection is
            # the only other thing that closes a segment, and it's
            # exactly what's stopped firing for someone now marked
            # SIGNED_OUT. Uses last_seen_at as the effective end time --
            # the same timestamp sign_out_time itself is derived from --
            # since that's the last moment this person was actually
            # confirmed present, not "now" (which could be arbitrarily
            # later than when they actually left).
            open_segment = (
                db.query(AttendanceSegment)
                .filter_by(org_id=rec.org_id, employee_id=rec.employee_id, date=rec.date, end_time=None)
                .order_by(AttendanceSegment.id.desc())
                .first()
            )
            if open_segment:
                start_dt = datetime.fromisoformat(f"{rec.date}T{open_segment.start_time}")
                open_segment.end_time = last_seen.time().isoformat(timespec="seconds")
                open_segment.duration_seconds = max((last_seen - start_dt).total_seconds(), 0)

            closed.append(rec.employee_id)
    db.commit()
    return {"closed_count": len(closed), "employee_ids": closed}
