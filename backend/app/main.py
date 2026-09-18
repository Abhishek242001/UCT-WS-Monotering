import logging
import os
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.logging_config import setup_logging

# Must run before anything else grabs a logger (routers/streams_ws.py does,
# at import time) so every logger in the process ends up pointed at the
# same file+console handlers instead of falling back to logging's default
# "no handlers found" console-only behavior.
setup_logging()
logger = logging.getLogger("app.main")

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
#
# Accepts a COMMA-SEPARATED list so local dev and a Lightning.ai Studio can
# both be allowed at once without editing .env every time you switch
# between them -- still an explicit allow-list, never a wildcard.
_frontend_origin_raw = os.environ.get("FRONTEND_ORIGIN", "http://localhost:8002")
_frontend_origins = [o.strip() for o in _frontend_origin_raw.split(",") if o.strip()]
if not _frontend_origins:
    _frontend_origins = ["http://localhost:8002"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request to backend/logs/backend.log, including the
    browser's Origin header and whether the response actually carries an
    Access-Control-Allow-Origin header -- the single fastest way to
    confirm, from the log file alone, whether a "CORS error" in the
    browser is really an origin mismatch (this middleware will show
    cors_header_present=NO) versus the backend being unreachable at all
    (nothing will be logged for that request).

    Added AFTER CORSMiddleware above so it wraps OUTSIDE it and therefore
    still sees rejected preflight (OPTIONS) requests, which CORSMiddleware
    answers directly without forwarding to the route handlers.
    """

    async def dispatch(self, request: Request, call_next):
        start = time.monotonic()
        origin = request.headers.get("origin", "-")
        response = None
        try:
            response = await call_next(request)
            return response
        except Exception:
            logger.exception("Unhandled error for %s %s", request.method, request.url.path)
            raise
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            status = response.status_code if response is not None else 599
            cors_note = ""
            if origin != "-":
                acao = response.headers.get("access-control-allow-origin") if response is not None else None
                cors_note = f" cors_header_present={'yes' if acao else 'NO (check FRONTEND_ORIGIN)'}"
            logger.info(
                "%s %s -> %s (%.1fms) origin=%s%s",
                request.method, request.url.path, status, duration_ms, origin, cors_note,
            )


app.add_middleware(RequestLoggingMiddleware)

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

# NOTE: "/" (used by the frontend's checkBackend() as a liveness check) is
# already defined in routers/streams.py's health() -- no separate root
# route needed here. (An earlier draft of this file added a duplicate
# "/" here on the mistaken assumption that no root route existed; it
# didn't -- streams.router's is included above and handles it.)


@app.on_event("startup")
def on_startup():
    logger.info("Allowed CORS origins (FRONTEND_ORIGIN): %s", _frontend_origins)
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
