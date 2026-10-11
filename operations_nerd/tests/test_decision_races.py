"""Two separate SQLite connections racing on the same pending action."""
import sys
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
sys.path.insert(0, str(Path(__file__).parents[1]))
from db import db as d
from approval.service import approve_and_send, reject_action, ApprovalError, maybe_auto_approve

@pytest.fixture
def pending(tmp_path):
    path = str(tmp_path / "decisions.db")
    d.init_db(path)
    with d.get_conn(path) as conn:
        bid = d.create_business(conn, "R", "realestate", "0.1.0", {})
        eid = d.create_event(conn, bid, "inbound_email", "hello")
        aid = d.create_drafted_action(conn, eid, bid, "reply", {"body": "Review me"})
        d.set_approval_policy(conn, bid, "reply", True)
    return path, aid

@pytest.mark.parametrize("second", [approve_and_send, reject_action, maybe_auto_approve])
def test_only_one_decision_wins(pending, second):
    path, aid = pending
    original = d.get_drafted_action
    gate = threading.Barrier(2)
    local = threading.local()
    def synchronized_read(conn, action_id):
        action = original(conn, action_id)
        if not getattr(local, "read", False):
            local.read = True
            gate.wait(timeout=3)
        return action
    def run(fn):
        try:
            with d.get_conn(path) as conn:
                result = fn(conn, aid)
            return "won" if result else "lost"
        except ApprovalError:
            return "lost"
    with patch("approval.service.d.get_drafted_action", side_effect=synchronized_read), \
         patch("approval.service._simulated_send") as send:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(run, approve_and_send)
            b = pool.submit(run, second)
            results = [a.result(), b.result()]
        assert results.count("won") == 1
        assert send.call_count <= 1
    with d.get_conn(path) as conn:
        action = original(conn, aid)
        assert action["status"] in {"sent", "rejected"}
        assert conn.execute("SELECT count(*) FROM audit_log WHERE action_id=?", (aid,)).fetchone()[0] == 1

def test_auto_policy_never_sends_rejected_action(pending):
    path, aid = pending
    with d.get_conn(path) as conn:
        reject_action(conn, aid)
    with patch("approval.service._simulated_send") as send:
        with d.get_conn(path) as conn:
            assert maybe_auto_approve(conn, aid) is None
        send.assert_not_called()
