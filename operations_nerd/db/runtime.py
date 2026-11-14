"""Request-local database selection.

Only the host's authenticated business resolver should enter database_scope().
The scope carries a connection target, not permission to access that target.
ContextVar tokens keep nested requests and worker threads from leaking routing
state. Auth/session code must use control_conn(), which ignores this scope.
"""

import os
from contextlib import contextmanager
from contextvars import ContextVar


_database_url: ContextVar[str | None] = ContextVar("operations_database_url", default=None)


def control_database_url() -> str | None:
    """Configured control database, or None for the legacy SQLite default."""
    return os.environ.get("OPERATIONS_DATABASE_URL") or None


def current_database_url() -> str | None:
    return _database_url.get() or control_database_url()


@contextmanager
def database_scope(url: str):
    """Temporarily route core data access to an already authorized database."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("A database scope needs a non-empty connection target")
    token = _database_url.set(url)
    try:
        yield
    finally:
        _database_url.reset(token)


def is_postgres_url(target: str) -> bool:
    return target.lower().startswith(("postgresql://", "postgres://"))
