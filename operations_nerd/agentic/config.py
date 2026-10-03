"""
Environment configuration for the agentic CRM and the startup checks.

    APP_ENV=production        turns on the strict startup checks below
    DATABASE_PATH             SQLite file (default: db/operations_nerd.db next to the code)
    ANTHROPIC_API_KEY         required in production
    ANTHROPIC_MODEL           optional (see anthropic_llm.py)
    SIGNUP_INVITE_CODE        signup is refused unless this is set and matches (or ALLOW_OPEN_SIGNUP=1)
    SESSION_SECRET            required in production, at least 32 characters
    PORT                      read by the process that starts uvicorn (see Dockerfile)
    CHAT_DAILY_LIMIT          assistant messages per account per UTC day (default 50)
    MAX_BODY_BYTES            largest accepted request body (default 1 MiB)
    TRUSTED_PROXIES           who may set X-Forwarded-Proto / X-Forwarded-For (default: nobody)
    RATE_LIMIT_ENABLED        set to 0 to turn the login/signup limiter off (tests, local dev)
"""

import ipaddress
import os

from db import db as d

DEFAULT_CHAT_DAILY_LIMIT = 50
DEFAULT_MAX_BODY_BYTES = 1024 * 1024
MIN_PRODUCTION_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised at startup with every configuration problem found."""


def is_production() -> bool:
    return os.environ.get("APP_ENV", "").strip().lower() == "production"


def _positive_int(name: str, default: int, problems: list[str] | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        if problems is not None:
            problems.append(f"{name} must be a positive whole number (got '{raw}')")
        return default


def chat_daily_limit() -> int:
    return _positive_int("CHAT_DAILY_LIMIT", DEFAULT_CHAT_DAILY_LIMIT)


def max_body_bytes() -> int:
    return _positive_int("MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)


def rate_limit_enabled() -> bool:
    return os.environ.get("RATE_LIMIT_ENABLED", "1").strip() != "0"


# ---------- reverse proxy ----------

def trusted_proxies() -> tuple[bool, list]:
    """(trust_everyone, networks_and_names). TRUSTED_PROXIES is a comma
    separated list of IPs, CIDR ranges, or "*". "*" is only safe when the
    container is reachable solely through the platform's proxy (the normal
    Railway/Render setup)."""
    raw = os.environ.get("TRUSTED_PROXIES", "")
    items = [i.strip() for i in raw.split(",") if i.strip()]
    if "*" in items:
        return True, []
    parsed = []
    for item in items:
        try:
            parsed.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            parsed.append(item)         # a plain host name, compared as text
    return False, parsed


def is_trusted_proxy(peer: str | None) -> bool:
    trust_all, trusted = trusted_proxies()
    if trust_all:
        return True
    if not peer or not trusted:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return peer in trusted
    return any(isinstance(t, (ipaddress.IPv4Network, ipaddress.IPv6Network)) and address in t
               for t in trusted)


# ---------- startup ----------

def check_environment() -> list[str]:
    """Every problem with the current environment, as readable strings."""
    problems: list[str] = []
    _positive_int("CHAT_DAILY_LIMIT", DEFAULT_CHAT_DAILY_LIMIT, problems)
    _positive_int("MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES, problems)
    if is_production():
        secret = os.environ.get("SESSION_SECRET", "")
        if not secret:
            problems.append("SESSION_SECRET is required when APP_ENV=production")
        elif len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
            problems.append(f"SESSION_SECRET must be at least {MIN_PRODUCTION_SECRET_LENGTH} characters")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            problems.append("ANTHROPIC_API_KEY is required when APP_ENV=production")
        if os.environ.get("CONFIG_AGENT_ENABLED", "").strip():
            problems.append("CONFIG_AGENT_ENABLED must not be set when APP_ENV=production "
                            "(it writes files into the pack folders)")
    return problems


def startup():
    """Called once when the app module is imported. Refuses to start on a bad
    configuration, then makes sure the database file and schema exist."""
    problems = check_environment()
    if problems:
        raise ConfigError("Refusing to start:\n" + "\n".join(f"  - {p}" for p in problems))
    directory = os.path.dirname(os.path.abspath(d.DB_PATH))
    os.makedirs(directory, exist_ok=True)
    d.init_db(d.DB_PATH)
