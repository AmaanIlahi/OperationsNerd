"""
Expired sessions are swept on login, and the lookup indexes exist.

Runs like the other tests here: python tests/test_session_pruning.py from
operations_nerd/.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["LLM_PROVIDER"] = "stub"
from db import db as d
from agentic import auth

PASSED = 0


def check(condition, message):
    global PASSED
    if not condition:
        raise AssertionError(message)
    PASSED += 1


def make_db():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    d.init_db(path, reset=True)
    return path


def add_account(conn, email):
    return conn.execute(
        "INSERT INTO accounts (email, password_hash) VALUES (?, ?)", (email, "x")
    ).lastrowid


def add_session(conn, account_id, token_hash, delta):
    expires = (auth._now() + delta).isoformat()
    conn.execute(
        "INSERT INTO sessions (account_id, token_hash, expires_at) VALUES (?, ?, ?)",
        (account_id, token_hash, expires),
    )


def test_login_sweeps_only_expired_sessions():
    path = make_db()
    with d.get_conn(path) as conn:
        account = add_account(conn, "a@example.com")
        add_session(conn, account, "old-1", timedelta(days=-2))
        add_session(conn, account, "old-2", timedelta(seconds=-1))
        add_session(conn, account, "live", timedelta(days=3))
        token = auth.create_session(conn, account)
        hashes = {r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")}
        check(hashes == {"live", auth._token_hash(token)}, f"unexpected sessions left: {hashes}")
        check(auth.account_for_token(conn, token)["email"] == "a@example.com", "new session must work")
    os.remove(path)


def test_sweep_covers_every_account_and_reports_count():
    path = make_db()
    with d.get_conn(path) as conn:
        first = add_account(conn, "a@example.com")
        second = add_account(conn, "b@example.com")
        add_session(conn, first, "old-a", timedelta(days=-1))
        add_session(conn, second, "old-b", timedelta(days=-1))
        add_session(conn, second, "live-b", timedelta(hours=1))
        check(auth.prune_expired_sessions(conn) == 2, "should delete both expired sessions")
        check(auth.prune_expired_sessions(conn) == 0, "second sweep has nothing to delete")
        left = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
        check(left == ["live-b"], f"live session must survive: {left}")
    os.remove(path)


def test_indexes_exist_and_are_used():
    path = make_db()
    conn = sqlite3.connect(path)
    names = {r[1] for r in conn.execute("SELECT type, name FROM sqlite_master WHERE type = 'index'")}
    for wanted in ("idx_sessions_expires", "idx_drafted_actions_event", "idx_events_contact"):
        check(wanted in names, f"missing index {wanted}")
    plan = " ".join(
        str(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM sessions WHERE expires_at <= '2026-01-01'"
        )
    )
    check("idx_sessions_expires" in plan, f"expiry sweep should use its index: {plan}")
    plan = " ".join(
        str(r) for r in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM drafted_actions WHERE event_id = 1")
    )
    check("idx_drafted_actions_event" in plan, f"event lookup should use its index: {plan}")
    conn.close()
    os.remove(path)


def test_existing_database_gets_the_new_indexes():
    path = make_db()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX idx_sessions_expires")
    conn.commit()
    conn.close()
    d.init_db(path)          # init without reset re-applies the schema
    conn = sqlite3.connect(path)
    names = {r[1] for r in conn.execute("SELECT type, name FROM sqlite_master WHERE type = 'index'")}
    check("idx_sessions_expires" in names, "init_db must add a missing index to an existing database")
    conn.close()
    os.remove(path)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("4 tests passed")
