from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AdminUser, AdminSession, AdminLoginAudit
from app import security

router = APIRouter(tags=["admin-auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LogoutRequest(BaseModel):
    session_token: str


@router.post("/admin/login")
def admin_login(req: LoginRequest, db: Session = Depends(get_db)):
    if security.is_locked_out(req.username):
        raise HTTPException(429, "Account temporarily locked due to repeated failed login attempts")

    user = db.get(AdminUser, req.username)
    if not user or not security.verify_password(req.password, user.password_hash, user.salt):
        security.record_failed_login(req.username)
        db.add(AdminLoginAudit(username=req.username, action="LOGIN"))
        db.commit()
        raise HTTPException(401, "Invalid username or password")

    security.clear_failed_logins(req.username)
    token = security.new_session_token()
    expires_at = (datetime.utcnow() + timedelta(seconds=security.SESSION_TTL_SECONDS)).isoformat()
    db.add(AdminSession(token=token, username=user.username, role=user.role, org_id=user.org_id, expires_at=expires_at))
    db.add(AdminLoginAudit(username=req.username, action="LOGIN"))
    db.commit()
    return {"status": "success", "session_token": token, "expires_at": expires_at, "role": user.role, "org_id": user.org_id}


@router.post("/admin/logout")
def admin_logout(req: LogoutRequest, db: Session = Depends(get_db)):
    sess = db.get(AdminSession, req.session_token)
    if sess:
        db.add(AdminLoginAudit(username=sess.username, action="LOGOUT"))
        db.delete(sess)
        db.commit()
    return {"status": "logged_out"}


def get_valid_session(token: str, db: Session) -> Optional[AdminSession]:
    """Shared validation logic used by both REST (Authorization header) and
    WebSocket (query-param token, since browsers cannot set custom headers
    on a WebSocket handshake) authentication."""
    token = (token or "").removeprefix("Bearer ").strip()
    if not token:
        return None
    sess = db.get(AdminSession, token)
    if not sess or sess.expires_at < datetime.utcnow().isoformat():
        return None
    return sess


@router.get("/admin/session")
def admin_session(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)):
    sess = get_valid_session(authorization or "", db)
    if not sess:
        return {"valid": False}
    return {"valid": True, "username": sess.username, "role": sess.role, "org_id": sess.org_id, "expires_at": sess.expires_at}


def require_admin(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> AdminSession:
    """Dependency other routers use to require a valid admin session via
    the Authorization header. For WebSocket routes (which cannot set
    custom headers from a browser), see get_valid_session() used directly
    with a query-param token instead -- see routers/streams_ws.py."""
    sess = get_valid_session(authorization or "", db)
    if not sess:
        raise HTTPException(401, "Authentication required")
    return sess


def verify_org_access(session: AdminSession, org_id: int) -> None:
    """The real fix underneath every individual cross-tenant bug found in
    this project's audit: every endpoint used to trust whatever org_id the
    CLIENT sent, rather than deriving it from who is actually logged in.
    That meant a logged-in admin could simply put a different org_id in
    their next request and act on it -- none of the individual
    entity-level checks (employee-belongs-to-shift, etc.) could prevent
    that, since they only catch MISMATCHES between two client-supplied
    values, not an admin freely choosing which org to operate as at all.

    role == "SUPER_ADMIN" is the one deliberate exception, for genuinely
    cross-org operations (e.g. a system-wide maintenance sweep) -- see
    admin_users.org_id's docstring in models.py."""
    if session.role == "SUPER_ADMIN":
        return
    if session.org_id != org_id:
        raise HTTPException(403, "This account is not authorized for the requested org_id")


class CreateAdminUserRequest(BaseModel):
    username: str
    password: str
    org_id: int  # required -- this endpoint creates real, org-scoped tenant accounts, not more bootstrap accounts


@router.post("/admin/users", status_code=201)
def create_admin_user(req: CreateAdminUserRequest, db: Session = Depends(get_db), admin: AdminSession = Depends(require_admin)):
    """Creates a genuinely org-scoped HR_ADMIN account. SUPER_ADMIN only --
    this is the one place that assigns which org an admin account can act
    on, so it can't be self-service for ordinary HR_ADMIN accounts."""
    if admin.role != "SUPER_ADMIN":
        raise HTTPException(403, "Only a SUPER_ADMIN account can create new admin users")
    if db.get(AdminUser, req.username):
        raise HTTPException(409, "Username already exists")
    password_hash, salt = security.hash_password(req.password)
    db.add(AdminUser(username=req.username, password_hash=password_hash, salt=salt, role="HR_ADMIN", org_id=req.org_id))
    db.commit()
    return {"status": "created", "username": req.username, "role": "HR_ADMIN", "org_id": req.org_id}
