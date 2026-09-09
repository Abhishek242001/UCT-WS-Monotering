"""
Real authentication primitives: PBKDF2 password hashing (stdlib only, no
compiled bcrypt dependency needed), random session tokens, and in-process
lockout tracking. Suitable for a self-hosted single-instance deployment;
a multi-instance production deployment should move lockout tracking into
the shared database or a cache like Redis instead of the in-memory dict
used here.
"""
import hashlib
import hmac
import os
import secrets
import time

PBKDF2_ITERATIONS = 260_000
LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW_SECONDS = 300
SESSION_TTL_SECONDS = 3600

_failed_logins: dict[str, list[float]] = {}


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITERATIONS)
    return digest.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def is_locked_out(username: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_logins.get(username, []) if now - t < LOCKOUT_WINDOW_SECONDS]
    _failed_logins[username] = attempts
    return len(attempts) >= LOCKOUT_THRESHOLD


def record_failed_login(username: str) -> None:
    _failed_logins.setdefault(username, []).append(time.time())


def clear_failed_logins(username: str) -> None:
    _failed_logins[username] = []
