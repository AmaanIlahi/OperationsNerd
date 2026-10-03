"""
In-process rate limiting for login and signup.

Deliberately simple: the counters live in this process's memory, so they
reset when the server restarts and are not shared between instances. That is
fine for a single-instance deployment; running several instances would need
a shared store (for example Redis) instead.

Each bucket is a sliding window of timestamps. Limits are module constants
so tests can tighten them; RATE_LIMIT_ENABLED=0 switches everything off.
"""

import threading
import time

from fastapi import HTTPException

from agentic import config

LOGIN_WINDOW = 15 * 60
LOGIN_MAX_PER_IP = 30            # login attempts from one address
LOGIN_MAX_FAILURES_PER_EMAIL = 5  # wrong passwords for one email
SIGNUP_WINDOW = 60 * 60
SIGNUP_MAX_PER_IP = 10
SIGNUP_MAX_PER_EMAIL = 3

_MAX_KEYS = 50_000

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def reset():
    with _lock:
        _hits.clear()


def _prune(key: str, window: float, now: float) -> list[float]:
    recent = [t for t in _hits.get(key, []) if now - t < window]
    if recent:
        _hits[key] = recent
    else:
        _hits.pop(key, None)
    return recent


def _too_many(retry_after: int, what: str) -> HTTPException:
    minutes = max(1, (retry_after + 59) // 60)
    return HTTPException(
        status_code=429,
        detail=f"Too many {what}. Please wait about {minutes} minute(s) and try again.",
        headers={"Retry-After": str(max(1, retry_after))},
    )


def _check(key: str, limit: int, window: float, what: str):
    now = time.monotonic()
    recent = _prune(key, window, now)
    if len(recent) >= limit:
        raise _too_many(int(window - (now - recent[0])), what)


def _record(key: str, window: float):
    now = time.monotonic()
    if len(_hits) > _MAX_KEYS:                      # bound memory under a flood of distinct keys
        for k in list(_hits):
            _prune(k, max(LOGIN_WINDOW, SIGNUP_WINDOW), now)
    _hits.setdefault(key, []).append(now)


# ---------- login ----------

def check_login(ip: str, email: str):
    """Call before checking the password. Counts the attempt against the IP."""
    if not config.rate_limit_enabled():
        return
    with _lock:
        _check(f"login:fail:{email}", LOGIN_MAX_FAILURES_PER_EMAIL, LOGIN_WINDOW, "failed login attempts")
        _check(f"login:ip:{ip}", LOGIN_MAX_PER_IP, LOGIN_WINDOW, "login attempts")
        _record(f"login:ip:{ip}", LOGIN_WINDOW)


def login_failed(email: str):
    if config.rate_limit_enabled():
        with _lock:
            _record(f"login:fail:{email}", LOGIN_WINDOW)


def login_succeeded(email: str):
    with _lock:
        _hits.pop(f"login:fail:{email}", None)


# ---------- signup ----------

def check_signup(ip: str, email: str):
    """Call first, before the invite code is looked at, so guessing the code
    is limited too. Every attempt counts."""
    if not config.rate_limit_enabled():
        return
    with _lock:
        _check(f"signup:ip:{ip}", SIGNUP_MAX_PER_IP, SIGNUP_WINDOW, "signup attempts")
        _check(f"signup:email:{email}", SIGNUP_MAX_PER_EMAIL, SIGNUP_WINDOW, "signup attempts for this email")
        _record(f"signup:ip:{ip}", SIGNUP_WINDOW)
        _record(f"signup:email:{email}", SIGNUP_WINDOW)
