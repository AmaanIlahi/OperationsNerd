"""
Accounts and sessions for the agentic CRM.

Passwords are hashed with bcrypt. A session is a random token held only in
the cookie; the database stores a keyed hash of it, so a leaked database
cannot be replayed as logins.
"""

import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, Request, Response

from agentic import config
from db import db as d

logger = logging.getLogger(__name__)
_generated_secret: bytes | None = None

SESSION_COOKIE = "session"
SESSION_DAYS = 7
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_BYTES = 72          # bcrypt ignores (or rejects) anything longer
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "testserver"}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Verified against when the email is unknown, so login takes about the same
# time whether or not the account exists.
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt())


def normalize_email(email: str) -> str:
    return email.strip().lower()


def check_email(email: str) -> bool:
    return len(email) <= 254 and bool(_EMAIL_RE.match(email))


def check_password(password: str) -> str | None:
    """Returns a problem description, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return f"Password must be at most {MAX_PASSWORD_BYTES} bytes"
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    stored = password_hash.encode("ascii") if password_hash else _DUMMY_HASH
    try:
        ok = bcrypt.checkpw(password.encode("utf-8")[:MAX_PASSWORD_BYTES], stored)
    except ValueError:
        return False
    return ok and password_hash is not None


def invite_code_ok(supplied: str | None) -> bool:
    """Signup is closed by default. It is allowed when SIGNUP_INVITE_CODE is
    set and the supplied code matches, or when ALLOW_OPEN_SIGNUP=1 and no
    invite code is configured."""
    required = os.environ.get("SIGNUP_INVITE_CODE")
    if required:
        return hmac.compare_digest((supplied or "").encode("utf-8"), required.encode("utf-8"))
    return os.environ.get("ALLOW_OPEN_SIGNUP") == "1"


# ---------- sessions ----------

def _session_key() -> bytes:
    """SESSION_SECRET if set. Otherwise a random per-process secret is
    generated (with a warning), so sessions stop working on restart rather
    than being hashed with a guessable key."""
    global _generated_secret
    configured = os.environ.get("SESSION_SECRET")
    if configured:
        return configured.encode("utf-8")
    if _generated_secret is None:
        _generated_secret = secrets.token_bytes(32)
        logger.warning("SESSION_SECRET is not set; using a random secret for this process. "
                       "Sessions will be invalidated on restart.")
    return _generated_secret


def _token_hash(token: str) -> str:
    return hmac.new(_session_key(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_session(conn, account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (account_id, token_hash, expires_at) VALUES (?, ?, ?)",
        (account_id, _token_hash(token), expires.isoformat()),
    )
    return token


def delete_session(conn, token: str):
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def account_for_token(conn, token: str) -> dict | None:
    row = conn.execute(
        """SELECT a.id, a.email, s.id AS session_id, s.expires_at
           FROM sessions s JOIN accounts a ON a.id = s.account_id
           WHERE s.token_hash = ?""",
        (_token_hash(token),),
    ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        conn.execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
        return None
    return {"id": row["id"], "email": row["email"]}


def _is_local(request: Request) -> bool:
    return (request.url.hostname or "") in LOCAL_HOSTS


def _peer(request: Request) -> str | None:
    return request.client.host if request.client else None


def effective_scheme(request: Request) -> str:
    """http or https as the browser saw it. When TLS is ended by a platform
    proxy the app itself only sees http, so X-Forwarded-Proto is honoured --
    but only when the connection really comes from a trusted proxy
    (TRUSTED_PROXIES); from anyone else the header is ignored."""
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded and config.is_trusted_proxy(_peer(request)):
        return forwarded.split(",")[0].strip().lower()
    return request.url.scheme


def is_secure_request(request: Request) -> bool:
    """Whether the session cookie gets the Secure flag: always in production,
    whenever the browser is on https, and otherwise whenever this is not a
    local address."""
    return config.is_production() or effective_scheme(request) == "https" or not _is_local(request)


def client_ip(request: Request) -> str:
    """The caller's address for rate limiting. Behind a trusted proxy that is
    the right-most X-Forwarded-For entry that is not itself a trusted proxy
    (the proxy appends the real client there); otherwise the socket peer."""
    peer = _peer(request) or "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded or not config.is_trusted_proxy(peer):
        return peer
    for candidate in reversed([p.strip() for p in forwarded.split(",") if p.strip()]):
        trust_all, _ = config.trusted_proxies()
        if trust_all or not config.is_trusted_proxy(candidate):
            return candidate
    return peer


def set_session_cookie(request: Request, response: Response, token: str):
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=is_secure_request(request),
        path="/",
    )


def clear_session_cookie(request: Request, response: Response):
    response.delete_cookie(
        SESSION_COOKIE, path="/", httponly=True, samesite="lax",
        secure=is_secure_request(request),
    )


def current_account(request: Request) -> dict:
    """FastAPI dependency: the logged-in account, or 401."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with d.get_conn() as conn:
            account = account_for_token(conn, token)
        if account:
            return account
    raise HTTPException(status_code=401, detail="Not logged in")


CurrentAccount = Depends(current_account)


# Resolve the key at import (app startup) so the warning appears immediately.
_session_key()
