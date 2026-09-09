"""
SQLAlchemy ORM models mirroring the 17-table schema documented in Section 7
of the project documentation. Grouped the same way: video/workstation
config, employee identity & assignment, attendance/shifts/admin.
"""
import json
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, ForeignKey, UniqueConstraint,
    CheckConstraint, DateTime,
)
from sqlalchemy.orm import relationship

from app.database import Base


def now_iso() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# 7.1 Video & Workstation Configuration
# ---------------------------------------------------------------------------
class Workstation(Base):
    __tablename__ = "workstations"
    workstation_id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    x1 = Column(Float, nullable=False)
    y1 = Column(Float, nullable=False)
    x2 = Column(Float, nullable=False)
    y2 = Column(Float, nullable=False)
    created_at = Column(String, default=now_iso)
    __table_args__ = (UniqueConstraint("org_id", "cam_id", "name"),)


class WorkstationVacancyLog(Base):
    __tablename__ = "workstation_vacancy_logs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    workstation_name = Column(String, nullable=False)
    analytics_date = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String)
    duration_seconds = Column(Float)
    created_at = Column(String, default=now_iso)


class WorkstationDailyAnalytics(Base):
    __tablename__ = "workstation_daily_analytics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    workstation_name = Column(String, nullable=False)
    analytics_date = Column(String, nullable=False)
    active_seconds = Column(Float, default=0)
    vacant_seconds = Column(Float, default=0)
    utilization_percent = Column(Float, default=0)
    missing_count = Column(Integer, default=0)
    missing_duration = Column(Float, default=0)
    first_seen_time = Column(String)
    last_present_time = Column(String)
    updated_at = Column(String, default=now_iso)
    __table_args__ = (UniqueConstraint("org_id", "cam_id", "workstation_name", "analytics_date"),)


# ---------------------------------------------------------------------------
# 7.2 Employee Identity & Assignment
# ---------------------------------------------------------------------------
class Employee(Base):
    __tablename__ = "employees"
    employee_id = Column(String, primary_key=True)
    org_id = Column(Integer, nullable=False)
    name = Column(String, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(String, default=now_iso)

    gallery = relationship("EmployeeFaceGallery", back_populates="employee", cascade="all, delete-orphan")


class EmployeeFaceGallery(Base):
    __tablename__ = "employee_face_gallery"
    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String, ForeignKey("employees.employee_id", ondelete="CASCADE"), nullable=False)
    view = Column(String, nullable=False)
    embedding = Column(Text)  # JSON-encoded float list on SQLite; use native vector(512) via pgvector on Postgres in production
    reference_depth_m = Column(Float)
    reference_width_px = Column(Float)
    created_at = Column(String, default=now_iso)
    __table_args__ = (
        UniqueConstraint("employee_id", "view"),
        CheckConstraint("view IN ('front','left','right','top')"),
    )

    employee = relationship("Employee", back_populates="gallery")

    def set_embedding(self, vec):
        self.embedding = json.dumps(list(vec))

    def get_embedding(self):
        return json.loads(self.embedding) if self.embedding else None


class WorkstationAssignment(Base):
    __tablename__ = "workstation_assignments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    workstation_name = Column(String, nullable=False)
    employee_id = Column(String, ForeignKey("employees.employee_id"), nullable=False)
    effective_from = Column(String, nullable=False)
    effective_to = Column(String)


class WorkstationIdentityEvent(Base):
    __tablename__ = "workstation_identity_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    workstation_name = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    assigned_employee_id = Column(String)
    detected_employee_id = Column(String)
    similarity = Column(Float)
    snr = Column(Float)
    distance_m = Column(Float)
    start_time = Column(String)
    end_time = Column(String)
    duration_seconds = Column(Float)
    created_at = Column(String, default=now_iso)
    __table_args__ = (CheckConstraint("event_type IN ('MATCH','MISMATCH','UNKNOWN','VACANT')"),)


class WorkstationIdentityDailyAnalytics(Base):
    __tablename__ = "workstation_identity_daily_analytics"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    workstation_name = Column(String, nullable=False)
    analytics_date = Column(String, nullable=False)
    matched_seconds = Column(Float, default=0)
    mismatched_seconds = Column(Float, default=0)
    unknown_seconds = Column(Float, default=0)
    vacant_seconds = Column(Float, default=0)
    utilization_percent = Column(Float, default=0)
    __table_args__ = (UniqueConstraint("org_id", "cam_id", "workstation_name", "analytics_date"),)


# ---------------------------------------------------------------------------
# 7.3 Attendance, Shifts & Administration
# ---------------------------------------------------------------------------
class AdminUser(Base):
    __tablename__ = "admin_users"
    username = Column(String, primary_key=True)
    password_hash = Column(String, nullable=False)
    salt = Column(String, nullable=False)
    role = Column(String, default="HR_ADMIN")
    org_id = Column(Integer, nullable=True)
    # org_id is nullable specifically for role == "SUPER_ADMIN" -- a
    # deliberately narrow escape hatch for genuinely cross-org operations
    # (e.g. close_stale_sessions, a system-wide maintenance sweep). Every
    # ordinary HR_ADMIN account must have an org_id; see verify_org_access()
    # in routers/admin_auth.py for how this is actually enforced.


class AdminLoginAudit(Base):
    __tablename__ = "admin_login_audit"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, nullable=False)
    action = Column(String, nullable=False)
    ip_address = Column(String)
    created_at = Column(String, default=now_iso)
    __table_args__ = (CheckConstraint("action IN ('LOGIN','LOGOUT')"),)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token = Column(String, primary_key=True)
    username = Column(String, nullable=False)
    role = Column(String, default="HR_ADMIN")
    org_id = Column(Integer, nullable=True)  # copied from AdminUser at login time; see admin_users.org_id note above
    expires_at = Column(String, nullable=False)


class EmployeeAttendance(Base):
    __tablename__ = "employee_attendance"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    employee_id = Column(String, ForeignKey("employees.employee_id"), nullable=False)
    date = Column(String, nullable=False)
    sign_in_time = Column(String)
    sign_out_time = Column(String)
    last_seen_at = Column(String)
    last_seen_cam_id = Column(Integer)
    last_seen_workstation = Column(String)
    status = Column(String, nullable=False, default="PRESENT")
    net_present_seconds = Column(Integer)
    day_classification = Column(String)
    __table_args__ = (
        UniqueConstraint("org_id", "employee_id", "date"),
        CheckConstraint("status IN ('PRESENT','ON_BREAK','SIGNED_OUT')"),
    )


class AttendanceSegment(Base):
    __tablename__ = "attendance_segments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    employee_id = Column(String, ForeignKey("employees.employee_id"), nullable=False)
    date = Column(String, nullable=False)
    cam_id = Column(Integer)
    department_or_workstation = Column(String)
    start_time = Column(String)
    end_time = Column(String)
    duration_seconds = Column(Float)


class Shift(Base):
    __tablename__ = "shifts"
    shift_id = Column(String, primary_key=True)
    org_id = Column(Integer, nullable=False)
    shift_name = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)


class BreakSchedule(Base):
    __tablename__ = "break_schedules"
    break_id = Column(String, primary_key=True)
    org_id = Column(Integer, nullable=False)
    shift_id = Column(String, ForeignKey("shifts.shift_id"), nullable=False)
    break_name = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)


class EmployeeShiftAssignment(Base):
    __tablename__ = "employee_shift_assignment"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    employee_id = Column(String, ForeignKey("employees.employee_id"), nullable=False)
    shift_id = Column(String, ForeignKey("shifts.shift_id"), nullable=False)
    effective_from = Column(String, nullable=False)
    effective_to = Column(String)


class OrgCalendar(Base):
    __tablename__ = "org_calendar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    org_id = Column(Integer, nullable=False)
    date = Column(String, nullable=False)
    label = Column(String)
    type = Column(String, nullable=False)
    __table_args__ = (
        UniqueConstraint("org_id", "date"),
        CheckConstraint("type IN ('HOLIDAY','WEEKLY_OFF')"),
    )


class AttendanceException(Base):
    __tablename__ = "attendance_exceptions"
    exception_id = Column(String, primary_key=True)
    org_id = Column(Integer, nullable=False)
    employee_id = Column(String, ForeignKey("employees.employee_id"), nullable=False)
    date = Column(String, nullable=False)
    type = Column(String, nullable=False)
    reason = Column(Text)
    admin_user = Column(String, nullable=False)
    created_at = Column(String, default=now_iso)
    __table_args__ = (CheckConstraint("type IN ('FIELD_WORK','LEAVE','WFH','CORRECTION')"),)


class SystemHealthEvent(Base):
    __tablename__ = "system_health_events"
    outage_id = Column(String, primary_key=True)
    org_id = Column(Integer, nullable=False)
    cam_id = Column(Integer, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String)
    reason = Column(Text)
