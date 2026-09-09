import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Shift, BreakSchedule, EmployeeShiftAssignment, OrgCalendar, Employee, AdminSession
from app.routers.admin_auth import require_admin, verify_org_access

router = APIRouter(tags=["shifts-breaks-calendar"])


class ShiftRequest(BaseModel):
    org_id: int
    shift_name: str
    start_time: str
    end_time: str


@router.post("/shifts", status_code=201)
def create_shift(req: ShiftRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    shift_id = f"SHIFT-{uuid.uuid4().hex[:6]}"
    db.add(Shift(shift_id=shift_id, org_id=req.org_id, shift_name=req.shift_name,
                 start_time=req.start_time, end_time=req.end_time))
    db.commit()
    return {"status": "created", "shift_id": shift_id}


@router.get("/shifts")
def list_shifts(org_id: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    rows = db.query(Shift).filter_by(org_id=org_id).all()
    return {"shifts": [{"shift_id": r.shift_id, "shift_name": r.shift_name,
                         "start_time": r.start_time, "end_time": r.end_time} for r in rows]}


class BreakRequest(BaseModel):
    org_id: int
    shift_id: str
    break_name: str
    start_time: str
    end_time: str


@router.post("/breaks", status_code=201)
def create_break(req: BreakRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    shift = db.get(Shift, req.shift_id)
    if not shift or shift.org_id != req.org_id:
        # shift_id is a globally unique primary key -- without this check,
        # org B could attach a break window to org A's shift just by
        # knowing/guessing its shift_id.
        raise HTTPException(404, "Shift not found")
    break_id = f"BRK-{uuid.uuid4().hex[:6]}"
    db.add(BreakSchedule(break_id=break_id, org_id=req.org_id, shift_id=req.shift_id,
                          break_name=req.break_name, start_time=req.start_time, end_time=req.end_time))
    db.commit()
    return {"status": "created", "break_id": break_id}


@router.get("/breaks")
def list_breaks(org_id: int, shift_id: str, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    rows = db.query(BreakSchedule).filter_by(org_id=org_id, shift_id=shift_id).all()
    return {"breaks": [{"break_id": r.break_id, "break_name": r.break_name,
                         "start_time": r.start_time, "end_time": r.end_time} for r in rows]}


class ShiftAssignRequest(BaseModel):
    org_id: int
    shift_id: str
    effective_from: str


@router.post("/employees/{employee_id}/shift-assignment")
def assign_shift(employee_id: str, req: ShiftAssignRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    employee = db.get(Employee, employee_id)
    if not employee or employee.org_id != req.org_id:
        raise HTTPException(404, "Employee not found")
    shift = db.get(Shift, req.shift_id)
    if not shift or shift.org_id != req.org_id:
        # Both employee_id and shift_id are globally unique primary keys.
        # The previous version checked only that each existed SOMEWHERE,
        # never that either belonged to req.org_id, or to the same org as
        # each other -- org B could freely link org A's employee to org
        # A's (or even org C's) shift while operating "as" org B.
        raise HTTPException(404, "Shift not found")
    db.add(EmployeeShiftAssignment(org_id=req.org_id, employee_id=employee_id, shift_id=req.shift_id,
                                    effective_from=req.effective_from))
    db.commit()
    return {"status": "assigned"}


class CalendarEntryRequest(BaseModel):
    org_id: int
    date: str
    label: str
    type: str


VALID_CALENDAR_TYPES = {"HOLIDAY", "WEEKLY_OFF"}


@router.post("/org-calendar", status_code=201)
def add_calendar_entry(req: CalendarEntryRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, req.org_id)
    if req.type not in VALID_CALENDAR_TYPES:
        raise HTTPException(422, f"type must be one of {VALID_CALENDAR_TYPES}")
    db.add(OrgCalendar(org_id=req.org_id, date=req.date, label=req.label, type=req.type))
    db.commit()
    return {"status": "added"}


@router.get("/org-calendar")
def get_calendar(org_id: int, year: int, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    verify_org_access(admin, org_id)
    rows = db.query(OrgCalendar).filter_by(org_id=org_id).filter(OrgCalendar.date.like(f"{year}%")).all()
    return {"calendar": [{"date": r.date, "label": r.label, "type": r.type} for r in rows]}
