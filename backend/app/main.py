import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import init_db
from app import database as db_module
from app.models import AdminUser
from app import security
from app.routers import (
    streams, workstations, employees, dataset_calibration, attendance,
    admin_auth, shifts_breaks_calendar, reports_and_health, videos, streams_ws,
)

app = FastAPI(
    title="UCT Workstation Monitoring API",
    description="Employee Identity-Aware Workstation Monitoring & Attendance System",
    version="0.1.0",
)

# Environment-aware CORS: set FRONTEND_ORIGIN explicitly (e.g. your
# Lightning.ai frontend subdomain, or http://localhost:8002 for local dev).
# Never use "*" once credentials/session cookies are in play (Section 10.3
# of the project documentation).
_frontend_origin = os.environ.get("FRONTEND_ORIGIN", "http://localhost:8002")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(streams.router)
app.include_router(workstations.router)
app.include_router(employees.router)
app.include_router(dataset_calibration.router)
app.include_router(attendance.router)
app.include_router(admin_auth.router)
app.include_router(shifts_breaks_calendar.router)
app.include_router(reports_and_health.router)
app.include_router(videos.router)
app.include_router(streams_ws.router)


@app.on_event("startup")
def on_startup():
    init_db()
    _seed_default_admin()


def _seed_default_admin():
    """Creates a default admin login on first run so there's a way into the
    dashboard at all. CHANGE THIS PASSWORD after first login -- see
    README.md. Controlled by env vars so it's not hardcoded in source.

    Seeded as SUPER_ADMIN (org_id=None, unrestricted) rather than an
    org-bound HR_ADMIN: this is a bootstrap/dev credential, not a real
    tenant account, and no admin-creation flow exists yet to make a
    genuinely org-scoped account except via this same seeding path. Real
    per-tenant admins should be created with a specific org_id and role
    HR_ADMIN via POST /admin/users (SUPER_ADMIN only), which then IS
    enforced by verify_org_access() on every org-scoped endpoint."""
    db = db_module.SessionLocal()
    try:
        username = os.environ.get("DEFAULT_ADMIN_USERNAME", "hr_admin1")
        if db.get(AdminUser, username):
            return
        password = os.environ.get("DEFAULT_ADMIN_PASSWORD", "ChangeMe123!")
        password_hash, salt = security.hash_password(password)
        db.add(AdminUser(username=username, password_hash=password_hash, salt=salt, role="SUPER_ADMIN", org_id=None))
        db.commit()
        print(f"\n[setup] Seeded default admin user -> username: {username}  password: {password}  role: SUPER_ADMIN")
        print("[setup] Change this via DEFAULT_ADMIN_USERNAME / DEFAULT_ADMIN_PASSWORD env vars, or after first login.")
        print("[setup] This bootstrap account can act on any org_id. Create org-scoped HR_ADMIN accounts via POST /admin/users for real tenant isolation.\n")
    finally:
        db.close()
