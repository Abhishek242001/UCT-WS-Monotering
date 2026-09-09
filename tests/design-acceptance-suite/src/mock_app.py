"""
Mock/reference implementation of the full 35-endpoint API surface, backed by
in-memory storage. This exists so the pytest suite has something real to run
contract and integration tests against ahead of the full production
implementation (real YOLO/ADAR pipeline, real PostgreSQL). Business logic
(state machine transitions, SNR gate decisions) delegates to `src.logic`
wherever practical so the same tested functions back both the API and the
unit tests.

This is NOT the production implementation. It intentionally omits real
computer-vision inference, TensorRT/OpenVINO acceleration, and any actual
network calls -- those require real models and hardware and are covered by
skipped/marked performance tests instead (see tests/test_performance_optimization.py).
"""
from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="Workstation Monitoring Mock API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # deliberately empty by default -- see test_security.py
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory "database"
# ---------------------------------------------------------------------------
DB: dict = {
    "streams": {},
    "workstations": {},          # (org_id, cam_id) -> {name: {x1,y1,x2,y2}}
    "assignments": {},           # (org_id, cam_id, name) -> employee_id
    "employees": {},             # employee_id -> {...}
    "gallery_counts": {},        # employee_id -> int views
    "uploads": {},               # upload_id -> {...}
    "calibration_jobs": {},      # job_id -> {...}
    "attendance": {},            # (org_id, employee_id, date) -> {...}
    "shifts": {},                # shift_id -> {...}
    "breaks": {},                # break_id -> {...}
    "calendar": [],
    "exceptions": {},
    "outages": {},
    "admin_users": {"hr_admin1": "correct-horse-battery-staple"},
    "admin_sessions": {},        # token -> {username, expires_at}
    "failed_logins": {},         # username -> [timestamps]
}

LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_SECONDS = 300


def _reset_db():
    """Test-only helper to reset all in-memory state between tests."""
    for k in list(DB.keys()):
        if isinstance(DB[k], dict):
            DB[k].clear()
        elif isinstance(DB[k], list):
            DB[k].clear()
    DB["admin_users"]["hr_admin1"] = "correct-horse-battery-staple"


# ---------------------------------------------------------------------------
# 8.1 Video & Stream Management
# ---------------------------------------------------------------------------
class StreamStartRequest(BaseModel):
    source: str
    org_id: int
    cam_id: int
    user_id: int
    use_nvenc: bool = False


@app.get("/")
def health():
    return {"status": "ok", "service": "workstation-monitoring"}


@app.post("/streams/start", status_code=201)
def streams_start(req: StreamStartRequest):
    if not req.source.strip():
        raise HTTPException(422, "source must not be empty")
    stream_id = str(uuid.uuid4())
    DB["streams"][stream_id] = req.model_dump()
    return {"stream_id": stream_id, "hls_url": f"/hls/{stream_id}/playlist.m3u8", "status": "started"}


class StreamStopRequest(BaseModel):
    stream_id: str


@app.post("/streams/stop")
def streams_stop(req: StreamStopRequest):
    if req.stream_id not in DB["streams"]:
        raise HTTPException(404, "Stream not found")
    del DB["streams"][req.stream_id]
    return {"status": "stopped", "stream_id": req.stream_id}


@app.get("/streams/list")
def streams_list():
    out = []
    for sid, s in DB["streams"].items():
        out.append({"stream_id": sid, "source": s["source"], "org_id": s["org_id"],
                    "cam_id": s["cam_id"], "hls_url": f"/hls/{sid}/playlist.m3u8"})
    return {"streams": out}


# ---------------------------------------------------------------------------
# 8.2 Workstation & ROI Management
# ---------------------------------------------------------------------------
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


@app.post("/workstations/save")
def workstations_save(req: WorkstationsSaveRequest):
    key = (req.org_id, req.cam_id)
    DB["workstations"].setdefault(key, {})
    for ws in req.workstations:
        DB["workstations"][key][ws.name] = ws.model_dump()
    return {"status": "saved", "org_id": req.org_id, "cam_id": req.cam_id, "count": len(req.workstations)}


@app.get("/workstations/check")
def workstations_check(org_id: int, cam_id: int):
    key = (org_id, cam_id)
    ws = DB["workstations"].get(key, {})
    return {"has_workstations": len(ws) > 0, "count": len(ws), "workstations": list(ws.values())}


class WorkstationDeleteRequest(BaseModel):
    org_id: int
    cam_id: int
    name: Optional[str] = None


@app.delete("/workstations/delete")
def workstations_delete(req: WorkstationDeleteRequest):
    key = (req.org_id, req.cam_id)
    ws = DB["workstations"].get(key, {})
    if req.name:
        if req.name not in ws:
            raise HTTPException(404, "No matching workstations found")
        del ws[req.name]
        return {"status": "deleted", "org_id": req.org_id, "cam_id": req.cam_id, "deleted_count": 1}
    count = len(ws)
    if count == 0:
        raise HTTPException(404, "No matching workstations found")
    ws.clear()
    return {"status": "deleted", "org_id": req.org_id, "cam_id": req.cam_id, "deleted_count": count}


class AssignRequest(BaseModel):
    org_id: int
    cam_id: int
    workstation_name: str
    employee_id: str
    effective_from: str


@app.post("/workstations/assign")
def workstations_assign(req: AssignRequest):
    if req.employee_id not in DB["employees"]:
        raise HTTPException(404, "Employee not found")
    key = (req.org_id, req.cam_id)
    if req.workstation_name not in DB["workstations"].get(key, {}):
        raise HTTPException(404, "Workstation not found")
    DB["assignments"][(req.org_id, req.cam_id, req.workstation_name)] = req.employee_id
    return {"status": "assigned", "org_id": req.org_id, "cam_id": req.cam_id,
            "workstation_name": req.workstation_name, "employee_id": req.employee_id}


@app.get("/workstations/identity_status")
def workstations_identity_status(org_id: int, cam_id: int):
    key = (org_id, cam_id)
    ws = DB["workstations"].get(key, {})
    out = []
    for name in ws:
        assigned = DB["assignments"].get((org_id, cam_id, name))
        out.append({
            "name": name, "occupancy_status": "VACANT", "assigned_employee_id": assigned,
            "detected_employee_id": None, "match_status": "VACANT",
            "similarity": None, "snr": None, "distance_m": None,
        })
    return {"org_id": org_id, "cam_id": cam_id, "workstations": out}


# ---------------------------------------------------------------------------
# 8.3 Employee & Enrollment Management
# ---------------------------------------------------------------------------
class Capture(BaseModel):
    view: str
    depth_m: float
    image_base64: str


class EnrollRequest(BaseModel):
    org_id: int
    employee_id: str
    name: str
    captures: list[Capture]


VALID_VIEWS = {"front", "left", "right", "top"}


@app.post("/employees/enroll", status_code=201)
def employees_enroll(req: EnrollRequest):
    views = {c.view for c in req.captures}
    if not views.issubset(VALID_VIEWS):
        raise HTTPException(422, f"Invalid view(s): {views - VALID_VIEWS}")
    if req.employee_id in DB["employees"]:
        raise HTTPException(409, "Employee already enrolled")
    DB["employees"][req.employee_id] = {
        "employee_id": req.employee_id, "org_id": req.org_id, "name": req.name,
        "active": True, "created_at": datetime.utcnow().isoformat(),
    }
    DB["gallery_counts"][req.employee_id] = len(req.captures)
    return {"status": "enrolled", "employee_id": req.employee_id,
            "views_captured": sorted(views), "calibration_pending": True}


@app.get("/employees/list")
def employees_list(org_id: int):
    return {"employees": [e for e in DB["employees"].values() if e["org_id"] == org_id]}


class EmployeeDeleteRequest(BaseModel):
    org_id: int
    employee_id: str


@app.delete("/employees/delete")
def employees_delete(req: EmployeeDeleteRequest):
    if req.employee_id not in DB["employees"]:
        raise HTTPException(404, "Employee not found")
    removed = DB["gallery_counts"].pop(req.employee_id, 0)
    del DB["employees"][req.employee_id]
    return {"status": "deleted", "employee_id": req.employee_id, "gallery_entries_removed": removed}


# ---------------------------------------------------------------------------
# 8.4 Dataset Upload & Calibration
# ---------------------------------------------------------------------------
DEPTH_RE = re.compile(r"^depth_(\d+(?:\.\d+)?)m$")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB -- see test_security.py for zip-bomb style tests


@app.post("/dataset/upload", status_code=201)
def dataset_upload(org_id: int, file_name: str, size_bytes: int):
    if not file_name.endswith(".zip"):
        raise HTTPException(422, "Only .zip uploads are accepted")
    if size_bytes <= 0:
        raise HTTPException(422, "size_bytes must be positive")
    if size_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Upload exceeds maximum allowed size")
    upload_id = f"UPL-{uuid.uuid4().hex[:12]}"
    DB["uploads"][upload_id] = {"org_id": org_id, "file_name": file_name, "size_bytes": size_bytes, "status": "received"}
    return {"upload_id": upload_id, "status": "received", "file_name": file_name, "size_bytes": size_bytes}


@app.get("/dataset/validate/{upload_id}")
def dataset_validate(upload_id: str):
    if upload_id not in DB["uploads"]:
        raise HTTPException(404, "Upload not found")
    # Mock validation always reports a clean pass for a well-formed upload_id.
    return {
        "upload_id": upload_id, "status": "PASSED", "errors": [], "warnings": [],
        "summary": {"people_found": 0, "calibration_volunteers_found": 0, "total_images": 0, "faces_not_detected": 0},
    }


class CalibrationRunRequest(BaseModel):
    org_id: int
    upload_id: str


@app.post("/calibration/run", status_code=202)
def calibration_run(req: CalibrationRunRequest):
    if req.upload_id not in DB["uploads"]:
        raise HTTPException(404, "Upload not found")
    job_id = f"CAL-JOB-{uuid.uuid4().hex[:8]}"
    DB["calibration_jobs"][job_id] = {"status": "COMPLETED", "org_id": req.org_id}
    return {"job_id": job_id, "status": "queued"}


@app.get("/calibration/status/{job_id}")
def calibration_status(job_id: str):
    job = DB["calibration_jobs"].get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "status": job["status"], "progress_percent": 100,
            "result": {"near_k": 0.21335, "near_sigma0": 0.0998, "near_gamma": 0.6270, "people_enrolled": 0}}


# ---------------------------------------------------------------------------
# 8.5 Attendance -- Employee Sign-in / Sign-out
# ---------------------------------------------------------------------------
@app.get("/attendance/today")
def attendance_today(org_id: int, employee_id: str):
    key = (org_id, employee_id, datetime.utcnow().date().isoformat())
    rec = DB["attendance"].get(key)
    if not rec:
        raise HTTPException(404, "No attendance record for today")
    return rec


class SignEventRequest(BaseModel):
    org_id: int
    employee_id: str
    timestamp: str  # ISO datetime, mock-only convenience field to drive state


@app.post("/attendance/_test_record_detection", include_in_schema=False)
def _test_record_detection(req: SignEventRequest):
    """Test-only helper endpoint (not part of the public API surface) used
    to drive attendance records deterministically in integration tests."""
    date = req.timestamp[:10]
    key = (req.org_id, req.employee_id, date)
    rec = DB["attendance"].get(key)
    if not rec:
        rec = {"org_id": req.org_id, "employee_id": req.employee_id, "date": date,
               "sign_in_time": req.timestamp[11:19], "sign_out_time": None, "status": "PRESENT",
               "last_seen_at": req.timestamp}
        DB["attendance"][key] = rec
    else:
        rec["last_seen_at"] = req.timestamp
        rec["status"] = "PRESENT"
    return rec


@app.get("/attendance/report")
def attendance_report(org_id: int, employee_id: str, from_: str = None, to: str = None):
    days = [v for (o, e, d), v in DB["attendance"].items() if o == org_id and e == employee_id]
    return {"employee_id": employee_id, "days": days}


@app.get("/attendance/segments")
def attendance_segments(org_id: int, employee_id: str, date: str):
    return {"employee_id": employee_id, "date": date, "segments": []}


class ExceptionRequest(BaseModel):
    org_id: int
    employee_id: str
    date: str
    type: str
    reason: str
    admin_user: str


VALID_EXCEPTION_TYPES = {"FIELD_WORK", "LEAVE", "WFH", "CORRECTION"}


@app.post("/attendance/exception", status_code=201)
def attendance_exception(req: ExceptionRequest):
    if req.type not in VALID_EXCEPTION_TYPES:
        raise HTTPException(422, f"type must be one of {VALID_EXCEPTION_TYPES}")
    exception_id = f"EXC-{uuid.uuid4().hex[:8]}"
    DB["exceptions"][exception_id] = req.model_dump()
    return {"status": "recorded", "exception_id": exception_id}


# ---------------------------------------------------------------------------
# 8.6 Administrator Authentication
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    username: str
    password: str


SESSION_TTL_SECONDS = 3600


@app.post("/admin/login")
def admin_login(req: LoginRequest, request: Request):
    now = time.time()
    attempts = [t for t in DB["failed_logins"].get(req.username, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    DB["failed_logins"][req.username] = attempts
    if len(attempts) >= LOCKOUT_THRESHOLD:
        raise HTTPException(429, "Account temporarily locked due to repeated failed login attempts")

    real_password = DB["admin_users"].get(req.username)
    if real_password is None or not secrets.compare_digest(real_password, req.password):
        attempts.append(now)
        DB["failed_logins"][req.username] = attempts
        raise HTTPException(401, "Invalid username or password")

    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(seconds=SESSION_TTL_SECONDS)
    DB["admin_sessions"][token] = {"username": req.username, "expires_at": expires_at, "role": "HR_ADMIN"}
    DB["failed_logins"][req.username] = []
    return {"status": "success", "session_token": token, "expires_at": expires_at.isoformat(), "role": "HR_ADMIN"}


class LogoutRequest(BaseModel):
    session_token: str


@app.post("/admin/logout")
def admin_logout(req: LogoutRequest):
    DB["admin_sessions"].pop(req.session_token, None)
    return {"status": "logged_out"}


@app.get("/admin/session")
def admin_session(authorization: Optional[str] = Header(None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    sess = DB["admin_sessions"].get(token)
    if not sess or sess["expires_at"] < datetime.utcnow():
        return {"valid": False}
    return {"valid": True, "username": sess["username"], "role": sess["role"], "expires_at": sess["expires_at"].isoformat()}


# ---------------------------------------------------------------------------
# 8.7 Shift, Break & Calendar Configuration
# ---------------------------------------------------------------------------
class ShiftRequest(BaseModel):
    org_id: int
    shift_name: str
    start_time: str
    end_time: str


@app.post("/shifts", status_code=201)
def create_shift(req: ShiftRequest):
    shift_id = f"SHIFT-{uuid.uuid4().hex[:6]}"
    DB["shifts"][shift_id] = req.model_dump()
    return {"status": "created", "shift_id": shift_id}


@app.get("/shifts")
def list_shifts(org_id: int):
    return {"shifts": [dict(v, shift_id=k) for k, v in DB["shifts"].items() if v["org_id"] == org_id]}


class BreakRequest(BaseModel):
    org_id: int
    shift_id: str
    break_name: str
    start_time: str
    end_time: str


@app.post("/breaks", status_code=201)
def create_break(req: BreakRequest):
    if req.shift_id not in DB["shifts"]:
        raise HTTPException(404, "Shift not found")
    break_id = f"BRK-{uuid.uuid4().hex[:6]}"
    DB["breaks"][break_id] = req.model_dump()
    return {"status": "created", "break_id": break_id}


@app.get("/breaks")
def list_breaks(org_id: int, shift_id: str):
    return {"breaks": [dict(v, break_id=k) for k, v in DB["breaks"].items()
                        if v["org_id"] == org_id and v["shift_id"] == shift_id]}


class ShiftAssignRequest(BaseModel):
    org_id: int
    shift_id: str
    effective_from: str


@app.post("/employees/{employee_id}/shift-assignment")
def assign_shift(employee_id: str, req: ShiftAssignRequest):
    if employee_id not in DB["employees"]:
        raise HTTPException(404, "Employee not found")
    if req.shift_id not in DB["shifts"]:
        raise HTTPException(404, "Shift not found")
    return {"status": "assigned"}


class CalendarEntryRequest(BaseModel):
    org_id: int
    date: str
    label: str
    type: str


VALID_CALENDAR_TYPES = {"HOLIDAY", "WEEKLY_OFF"}


@app.post("/org-calendar", status_code=201)
def add_calendar_entry(req: CalendarEntryRequest):
    if req.type not in VALID_CALENDAR_TYPES:
        raise HTTPException(422, f"type must be one of {VALID_CALENDAR_TYPES}")
    DB["calendar"].append(req.model_dump())
    return {"status": "added"}


@app.get("/org-calendar")
def get_calendar(org_id: int, year: int):
    return {"calendar": [c for c in DB["calendar"] if c["org_id"] == org_id and c["date"].startswith(str(year))]}


# ---------------------------------------------------------------------------
# 8.8 Reporting
# ---------------------------------------------------------------------------
@app.get("/reports/desk-utilization")
def report_desk_utilization(org_id: int, cam_id: int, from_: str = None, to: str = None):
    return {"org_id": org_id, "cam_id": cam_id, "range": {"from": from_, "to": to}, "employees": []}


@app.get("/reports/mismatches")
def report_mismatches(org_id: int, cam_id: int, date: str):
    return {"org_id": org_id, "cam_id": cam_id, "date": date, "events": []}


# ---------------------------------------------------------------------------
# 8.9 System Health
# ---------------------------------------------------------------------------
@app.get("/system/health")
def system_health(org_id: int):
    return {"cameras": [], "pipeline_status": "HEALTHY"}


class OutageRequest(BaseModel):
    org_id: int
    cam_id: int
    start_time: str
    reason: str


@app.post("/system/outage", status_code=201)
def record_outage(req: OutageRequest):
    outage_id = f"OUT-{uuid.uuid4().hex[:6]}"
    DB["outages"][outage_id] = req.model_dump()
    return {"status": "recorded", "outage_id": outage_id}
