"""
HR module: leave types, allowance policy, accruals, the leave ledger, balance
and overlap enforcement, termination effects and idempotency. Tests marked
"raw" write SQL directly, bypassing hr.py, so whatever they see rejected is
rejected by the database itself. Run from the repo root with the venv (pytest
also collects these):

    python -B modules/hr/tests/test_leave.py
"""

import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr as h
from test_employees import dump, hire, new_db, rejected

YEAR = 2026
# 2026-11-02 is a Monday.
MON, TUE, WED, THU, FRI, SAT, SUN = (f"2026-11-0{d}" for d in range(2, 9))
# The day cancel_leave is told it is, so that cancelling approved leave doesn't depend on the clock.
TODAY = "2026-10-26"


def shop(name: str, allowance=20):
    """A database with an owner (1), an assistant (2) reporting to them, annual leave (tracked) and unpaid leave."""
    conn, path = new_db(name)
    hire(conn, "E001", first_name="Maya", job_title="Owner", start_date="2024-01-08")
    hire(conn, "E002", first_name="Sam", manager_id=1, start_date="2025-03-03")
    h.create_leave_type(conn, "annual", "Annual leave", "lt:annual", annual_allowance_days=allowance)
    h.create_leave_type(conn, "unpaid", "Unpaid leave", "lt:unpaid", paid=False, tracks_balance=False)
    return conn, path


def raw_ledger(conn, employee_id, type_id, entry_type, half_days, key, request_id=None, accrual_year=None,
               reason=None):
    return conn.execute("""INSERT INTO hr_leave_ledger (employee_id, leave_type_id, entry_type, half_days, request_id,
                                                        accrual_year, reason, idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (employee_id, type_id, entry_type, half_days, request_id, accrual_year, reason, key))


def raw_request(conn, employee_id, type_id, start, end, half_days, key, status="pending"):
    return conn.execute("""INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date, end_date, half_days,
                                                          status, idempotency_key)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""", (employee_id, type_id, start, end, half_days, status, key))


def bal(conn, employee_id=2, code="annual"):
    b = h.leave_balance(conn, employee_id, code)
    return b["balance"], b["pending"], b["available"]


# ---------------------------------------------------------------- types and policy

def test_leave_types_and_their_rules():
    conn, _ = shop("types")
    assert [t["code"] for t in h.leave_types(conn)] == ["annual", "unpaid"]
    assert h.leave_type(conn, "annual") == {"code": "annual", "name": "Annual leave", "paid": True,
                                            "tracks_balance": True, "annual_allowance_days": 20}
    assert h.leave_type(conn, "unpaid")["tracks_balance"] is False
    rejected(lambda: h.create_leave_type(conn, "annual", "Again", "k"), "already exists", h.InvalidState)
    rejected(lambda: h.create_leave_type(conn, "Sick Leave", "Sick", "k"), "lowercase letters", ValueError)
    rejected(lambda: h.create_leave_type(conn, "x", "X", "k", tracks_balance=False, annual_allowance_days=5),
             "has no allowance", ValueError)
    rejected(lambda: h.create_leave_type(conn, "x", "X", "k", annual_allowance_days=2.25), "whole or half day",
             ValueError)
    rejected(lambda: h.create_leave_type(conn, "x", "X", "k", annual_allowance_days=-1), "must be positive",
             ValueError)
    rejected(lambda: h.leave_type(conn, "sick"), "no leave type 'sick'", h.NotFound)


def test_an_allowance_change_affects_only_later_accruals():
    conn, _ = shop("policy")
    h.accrue_annual_leave(conn, 1, "annual", YEAR, "acc:1")
    h.set_leave_allowance(conn, "annual", 25, "new policy", "pol:1")
    assert h.leave_type(conn, "annual")["annual_allowance_days"] == 25
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc:2")
    assert h.leave_balance(conn, 1, "annual")["accrued"] == 20
    assert h.leave_balance(conn, 2, "annual")["accrued"] == 25
    history = [tuple(r) for r in conn.execute(
        "SELECT annual_allowance_half_days, reason FROM hr_leave_policy_changes ORDER BY id")]
    assert history == [(40, "initial allowance"), (50, "new policy")]
    rejected(lambda: h.set_leave_allowance(conn, "annual", 25, "same", "pol:2"), "already allows", ValueError)
    rejected(lambda: h.set_leave_allowance(conn, "unpaid", 5, "x", "pol:3"), "no balance", h.InvalidState)


def test_raw_leave_type_changes_go_through_a_policy_change():
    conn, _ = shop("raw_types")
    rejected(lambda: conn.execute("UPDATE hr_leave_types SET annual_allowance_half_days = 99 WHERE code = 'annual'"),
             "through a policy change")
    rejected(lambda: conn.execute("UPDATE hr_leave_types SET name = 'Holiday' WHERE code = 'annual'"),
             "through a policy change")
    rejected(lambda: conn.execute("DELETE FROM hr_leave_types"), "rows are kept")
    rejected(lambda: conn.execute("UPDATE hr_leave_policy_changes SET reason = 'x'"), "rows are immutable")
    rejected(lambda: conn.execute("""INSERT INTO hr_leave_policy_changes (leave_type_id, annual_allowance_half_days,
                                     reason, idempotency_key) VALUES (2, 10, 'x', 'raw')"""), "no balance, so no allowance")
    rejected(lambda: conn.execute("""INSERT INTO hr_leave_types (code, name, paid, tracks_balance,
                                     annual_allowance_half_days, idempotency_key) VALUES ('x', 'X', 1, 0, 4, 'raw2')"""),
             "CHECK constraint failed")


# ---------------------------------------------------------------- accrual

def test_accrual_is_once_a_year_and_prorated_from_the_start_date():
    conn, _ = shop("accrual")
    full = h.accrue_annual_leave(conn, 1, "annual", 2025, "acc:1:2025")
    assert full["days"] == 20 and full["prorated"] is False
    part = h.accrue_annual_leave(conn, 2, "annual", 2025, "acc:2:2025")      # started 3 March 2025: 304 of 365 days
    assert part["days"] == 16.5 and part["prorated"] is True                   # 40 * 304 // 365 = 33 half-days
    assert h.accrue_annual_leave(conn, 2, "annual", 2026, "acc:2:2026")["days"] == 20
    assert bal(conn) == (36.5, 0, 36.5)
    rejected(lambda: h.accrue_annual_leave(conn, 2, "annual", 2026, "again"), "already accrued", h.InvalidState)
    rejected(lambda: h.accrue_annual_leave(conn, 2, "annual", 2024, "early"), "after 2024", ValueError)
    rejected(lambda: h.accrue_annual_leave(conn, 2, "unpaid", 2026, "u"), "has no balance", h.InvalidState)
    rejected(lambda: h.accrue_annual_leave(conn, 2, "annual", "2026", "s"), "integer year", ValueError)
    rejected(lambda: h.accrue_annual_leave(conn, 9, "annual", 2026, "n"), "no employee 9", h.NotFound)


def test_adjustments_correct_a_balance_within_what_is_available():
    conn, _ = shop("adjust")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    h.adjust_leave(conn, 2, "annual", 2.5, "carried over from the old system", "adj:1", as_of="2026-01-05")
    h.request_leave(conn, 2, "annual", MON, FRI, "req")                       # 5 days pending
    assert bal(conn) == (22.5, 5, 17.5)
    before = dump(conn)
    rejected(lambda: h.adjust_leave(conn, 2, "annual", -18, "too much", "adj:2"), "17.5 'annual' days available",
             h.InsufficientBalance)
    rejected(lambda: h.adjust_leave(conn, 2, "annual", 0, "nothing", "adj:3"), "must be non-zero", ValueError)
    rejected(lambda: h.adjust_leave(conn, 2, "unpaid", 1, "x", "adj:4"), "has no balance", h.InvalidState)
    assert dump(conn) == before
    h.adjust_leave(conn, 2, "annual", -17.5, "correction", "adj:5")
    assert bal(conn) == (5, 5, 0)


# ---------------------------------------------------------------- requests and the ledger

def test_a_request_counts_working_days_and_holds_them_until_decided():
    conn, _ = shop("request")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    r = h.request_leave(conn, 2, "annual", FRI, "2026-11-10", "req:1", note="family visit")  # Fri..Tue: 3 working days
    assert r["days"] == 3 and r["status"] == "pending" and r["note"] == "family visit"
    assert bal(conn) == (20, 3, 17)
    half = h.request_leave(conn, 2, "annual", "2026-11-12", "2026-11-12", "req:2", half_day=True)
    assert half["days"] == 0.5
    assert bal(conn) == (20, 3.5, 16.5)
    rejected(lambda: h.request_leave(conn, 2, "annual", SAT, SUN, "k"), "has no working day", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", TUE, MON, "k"), "is before start_date", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", MON, TUE, "k", half_day=True), "half_day is for a single day",
             ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", "2025-01-06", "2025-01-06", "k"),
             "before the employee's start date", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "sick", MON, MON, "k"), "no leave type 'sick'", h.NotFound)
    assert [e["entry_type"] for e in h.leave_ledger(conn, 2)] == ["accrual", "request", "request"]
    assert h.working_days(MON, SUN) == 5 and h.working_days(MON, "2026-11-09") == 6 and h.working_days(MON, "2026-11-15") == 10


def test_approve_debits_reject_does_not_and_cancel_credits_an_approved_request():
    conn, _ = shop("decide")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    a = h.request_leave(conn, 2, "annual", MON, WED, "req:a")                 # 3 days
    b = h.request_leave(conn, 2, "annual", "2026-11-16", "2026-11-16", "req:b")
    c = h.request_leave(conn, 2, "annual", "2026-11-23", "2026-11-24", "req:c")
    assert bal(conn) == (20, 6, 14)
    assert h.approve_leave(conn, a["request_id"], "ok:a", approver="Maya", as_of="2026-10-26")["status"] == "approved"
    assert bal(conn) == (17, 3, 14)
    assert h.reject_leave(conn, b["request_id"], "stocktake that day", "no:b", approver="Maya")["status"] == "rejected"
    assert bal(conn) == (17, 2, 15)
    assert h.cancel_leave(conn, c["request_id"], "cx:c", reason="plans changed")["status"] == "cancelled"
    assert bal(conn) == (17, 0, 17)
    assert h.cancel_leave(conn, a["request_id"], "cx:a", today=TODAY)["status"] == "cancelled"
    assert bal(conn) == (20, 0, 20)
    b_ = h.leave_balance(conn, 2, "annual")
    assert b_["accrued"] == 20 and b_["taken"] == 0
    ledger = [(e["entry_type"], e["days"], e["request_id"]) for e in h.leave_ledger(conn, 2)]
    assert ledger == [("accrual", 20, None), ("request", 0, 1), ("request", 0, 2), ("request", 0, 3),
                      ("approve", -3, 1), ("reject", 0, 2), ("cancel", 0, 3), ("cancel", 3, 1)]
    for rid, step in ((a["request_id"], h.approve_leave), (b["request_id"], h.approve_leave)):
        rejected(lambda: step(conn, rid, f"again:{rid}"), "is cancelled" if rid == 1 else "is rejected",
                 h.InvalidState)
    rejected(lambda: h.cancel_leave(conn, b["request_id"], "cx:b"), "is rejected", h.InvalidState)
    rejected(lambda: h.reject_leave(conn, 99, "x", "k"), "no leave request 99", h.NotFound)
    rejected(lambda: h.reject_leave(conn, 1, "", "k"), "reason must be", ValueError)


def test_requests_beyond_the_available_balance_are_refused():
    conn, _ = shop("balance", allowance=5)
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    h.request_leave(conn, 2, "annual", MON, THU, "req:1")                     # 4 of 5 held
    before = dump(conn)
    rejected(lambda: h.request_leave(conn, 2, "annual", "2026-11-16", "2026-11-17", "req:2"),
             "1.0 available (5.0 balance, 4.0 pending)", h.InsufficientBalance)
    assert dump(conn) == before
    h.request_leave(conn, 2, "annual", "2026-11-16", "2026-11-16", "req:3")  # exactly the last day
    assert bal(conn) == (5, 5, 0)
    rejected(lambda: h.request_leave(conn, 2, "annual", "2026-11-17", "2026-11-17", "req:4", half_day=True),
             "0.0 available", h.InsufficientBalance)
    rejected(lambda: h.request_leave(conn, 1, "annual", MON, MON, "req:5"), "0.0 available", h.InsufficientBalance)


def test_untracked_leave_has_no_balance_and_is_only_checked_for_overlaps():
    conn, _ = shop("unpaid")
    r = h.request_leave(conn, 2, "unpaid", MON, FRI, "req:u")
    h.approve_leave(conn, r["request_id"], "ok:u")
    b = h.leave_balance(conn, 2, "unpaid")
    assert b["balance"] is None and b["available"] is None and b["taken"] == 5
    rejected(lambda: h.request_leave(conn, 2, "unpaid", FRI, "2026-11-10", "req:u2"), "overlaps approved request 1",
             h.Overlap)


def test_overlapping_requests_are_refused_across_leave_types_until_the_first_is_rejected_or_cancelled():
    conn, _ = shop("overlap")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    first = h.request_leave(conn, 2, "annual", TUE, THU, "req:1")
    for start, end in ((MON, TUE), (THU, FRI), (WED, WED), (MON, FRI)):
        rejected(lambda: h.request_leave(conn, 2, "annual", start, end, f"o:{start}{end}"),
                 "overlaps pending request 1", h.Overlap)
        rejected(lambda: h.request_leave(conn, 2, "unpaid", start, end, f"u:{start}{end}"),
                 "overlaps pending request 1", h.Overlap)
    h.request_leave(conn, 2, "annual", MON, MON, "req:mon")                   # adjacent days are fine
    h.request_leave(conn, 2, "unpaid", FRI, FRI, "req:fri")
    h.request_leave(conn, 1, "unpaid", TUE, THU, "req:boss")                  # another employee is fine
    h.approve_leave(conn, first["request_id"], "ok:1")
    rejected(lambda: h.request_leave(conn, 2, "annual", WED, WED, "o:2"), "overlaps approved request 1", h.Overlap)
    h.cancel_leave(conn, first["request_id"], "cx:1", today=TODAY)
    h.request_leave(conn, 2, "annual", WED, WED, "req:again")


def test_two_requests_racing_for_the_last_days_cannot_both_win():
    for trial in range(5):
        conn, path = shop(f"race_{trial}", allowance=3)
        h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
        results, barrier = [], threading.Barrier(2)

        def worker(start, end, key):
            c = h.connect(path)
            try:
                barrier.wait()
                results.append(h.request_leave(c, 2, "annual", start, end, key)["status"])
            except h.InsufficientBalance:
                results.append("refused")
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(MON, WED, "a")),
                   threading.Thread(target=worker, args=("2026-11-16", "2026-11-17", "b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == ["pending", "refused"], results
        assert bal(conn)[2] >= 0


# ---------------------------------------------------------------- termination effects

def test_termination_cancels_pending_and_future_leave_and_stops_new_leave():
    conn, _ = shop("term_leave")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    past = h.request_leave(conn, 2, "annual", "2026-10-05", "2026-10-06", "req:past")
    h.approve_leave(conn, past["request_id"], "ok:past")
    spanning = h.request_leave(conn, 2, "annual", "2026-11-12", "2026-11-13", "req:span")
    h.approve_leave(conn, spanning["request_id"], "ok:span")
    future = h.request_leave(conn, 2, "annual", "2026-12-01", "2026-12-02", "req:future")
    h.approve_leave(conn, future["request_id"], "ok:future")
    pending = h.request_leave(conn, 2, "annual", "2026-12-07", "2026-12-07", "req:pending")
    assert bal(conn) == (14, 1, 13)
    out = h.terminate_employee(conn, 2, "2026-11-12", "resigned", "term")
    assert out["cancelled_leave"] == [{"request_id": future["request_id"], "was": "approved", "credited_days": 2},
                                      {"request_id": pending["request_id"], "was": "pending", "credited_days": 0}]
    # The leave running past the last day is charged only up to it: Thursday kept, Friday credited back.
    assert out["shortened_leave"] == [{"request_id": spanning["request_id"], "replaced_by": 5, "kept_days": 1,
                                       "credited_days": 1}]
    statuses = {r["request_id"]: (r["status"], r["start_date"], r["end_date"])
                for r in h.leave_requests(conn, employee_id=2)}
    assert statuses == {past["request_id"]: ("approved", "2026-10-05", "2026-10-06"),
                        spanning["request_id"]: ("cancelled", "2026-11-12", "2026-11-13"),
                        5: ("approved", "2026-11-12", "2026-11-12"),
                        future["request_id"]: ("cancelled", "2026-12-01", "2026-12-02"),
                        pending["request_id"]: ("cancelled", "2026-12-07", "2026-12-07")}
    assert bal(conn) == (17, 0, 17)                                            # still readable after termination
    assert [(e["entry_type"], e["days"], e["reason"]) for e in h.leave_ledger(conn, 2)][-5:] == [
        ("cancel", 2, "employment ended"), ("cancel", 0, "employment ended"),
        ("cancel", 2, "employment ended on 2026-11-12"),
        ("request", 0, "the part of request 2 up to 2026-11-12"),
        ("approve", -1, "the part of request 2 up to 2026-11-12")]
    before = dump(conn)
    rejected(lambda: h.request_leave(conn, 2, "annual", "2026-12-14", "2026-12-14", "k1"), "is terminated",
             h.InvalidState)
    rejected(lambda: h.accrue_annual_leave(conn, 2, "annual", 2027, "k2"), "is terminated", h.InvalidState)
    rejected(lambda: h.adjust_leave(conn, 2, "annual", 1, "x", "k3"), "is terminated", h.InvalidState)
    # Nor is leave cancelled (and credited) after they've gone: termination settled it.
    rejected(lambda: h.cancel_leave(conn, 5, "cx:kept", today=TODAY), "is terminated", h.InvalidState)
    rejected(lambda: h.cancel_leave(conn, past["request_id"], "cx:past", today=TODAY), "is terminated",
             h.InvalidState)
    assert dump(conn) == before


def test_raw_leave_for_a_terminated_employee_is_refused():
    """Raw: the database itself refuses new requests, adjustments and accruals for someone who has left."""
    conn, _ = shop("term_raw")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    h.terminate_employee(conn, 2, "2026-10-30", "left", "term")
    rejected(lambda: raw_request(conn, 2, 1, MON, MON, 2, "raw"), "employee is not active")
    rejected(lambda: raw_ledger(conn, 2, 1, "adjustment", 2, "raw2", reason="x"), "employee is not active")
    rejected(lambda: raw_ledger(conn, 2, 1, "accrual", 2, "raw3", accrual_year=2027), "employee is not active")


# ---------------------------------------------------------------- idempotency

def test_leave_commands_replay_and_conflict_by_key():
    conn, _ = shop("leave_replay")
    acc = h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    before = dump(conn)
    assert h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc") == acc
    assert dump(conn) == before
    r = h.request_leave(conn, 2, "annual", MON, TUE, "req")
    assert h.request_leave(conn, 2, "annual", MON, TUE, "req") == r
    ok = h.approve_leave(conn, r["request_id"], "ok", approver="Maya")
    before = dump(conn)
    assert h.approve_leave(conn, r["request_id"], "ok", approver="Maya") == ok                 # retry: same answer
    assert dump(conn) == before
    rejected(lambda: h.approve_leave(conn, r["request_id"], "ok", approver="Someone else"), "already used for",
             h.IdempotencyConflict)
    rejected(lambda: h.request_leave(conn, 2, "annual", WED, WED, "req"), "already used for request_leave",
             h.IdempotencyConflict)
    rejected(lambda: h.cancel_leave(conn, r["request_id"], "acc"), "already used for accrue_annual_leave",
             h.IdempotencyConflict)
    adj = h.adjust_leave(conn, 2, "annual", 1, "bonus day", "adj")
    assert h.adjust_leave(conn, 2, "annual", 1, "bonus day", "adj") == adj
    assert bal(conn) == (19, 0, 19)


def test_bad_leave_input_is_rejected_before_anything_is_written():
    conn, _ = shop("bad_leave")
    before = dump(conn)
    for days in (True, "2", None, float("nan"), 0.3):
        rejected(lambda: h.adjust_leave(conn, 2, "annual", days, "x", "k"), "days must be", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", "2026-11-31", "2026-12-01", "k"), "start_date must be",
             ValueError)
    rejected(lambda: h.request_leave(conn, "2", "annual", MON, MON, "k"), "employee_id must be", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", MON, MON, "k", half_day=1), "half_day must be", ValueError)
    rejected(lambda: h.approve_leave(conn, 0, "k"), "request_id must be", ValueError)
    rejected(lambda: h.approve_leave(conn, 1, "k", as_of="soon"), "as_of must be", ValueError)
    assert dump(conn) == before
    assert not conn.in_transaction


# ---------------------------------------------------------------- raw: the database rules

def test_raw_ledger_rows_are_immutable_and_balances_cannot_go_negative():
    conn, _ = shop("raw_ledger", allowance=2)
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")                     # 4 half-days
    rejected(lambda: conn.execute("UPDATE hr_leave_ledger SET half_days = 400"), "hr_leave_ledger rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM hr_leave_ledger"), "hr_leave_ledger rows are immutable")
    # The adjustment rule fires first; "leave balance would go negative" is the backstop behind it (README).
    rejected(lambda: raw_ledger(conn, 2, 1, "adjustment", -5, "raw1", reason="x"), "adjustment exceeds available")
    rejected(lambda: raw_ledger(conn, 2, 1, "accrual", 4, "raw2", accrual_year=YEAR), "UNIQUE constraint failed")
    rejected(lambda: raw_ledger(conn, 2, 1, "accrual", -1, "raw3", accrual_year=2027), "CHECK constraint failed")
    rejected(lambda: raw_ledger(conn, 2, 1, "adjustment", 1, "raw4"), "CHECK constraint failed")     # no reason
    rejected(lambda: raw_ledger(conn, 2, 1, "accrual", 4, "raw5"), "CHECK constraint failed")         # no year
    rejected(lambda: raw_ledger(conn, 2, 2, "accrual", 4, "raw6", accrual_year=YEAR), "leave type has no balance")
    rejected(lambda: raw_ledger(conn, 2, 1, "bonus", 4, "raw7"), "CHECK constraint failed")
    h.request_leave(conn, 2, "annual", MON, MON, "req")                       # holds 2 of 4
    rejected(lambda: raw_ledger(conn, 2, 1, "adjustment", -3, "raw8", reason="x"),
             "adjustment exceeds available balance")


def test_raw_requests_follow_the_balance_overlap_and_status_rules():
    conn, _ = shop("raw_requests", allowance=2)
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    rejected(lambda: raw_request(conn, 2, 1, MON, WED, 6, "r1"), "leave request exceeds available balance")
    rejected(lambda: raw_request(conn, 2, 1, MON, MON, 2, "r2", status="approved"), "a leave request starts pending")
    rejected(lambda: raw_request(conn, 2, 1, MON, MON, 3, "r3"), "CHECK constraint failed")          # 1.5 days in 1
    rejected(lambda: raw_request(conn, 2, 1, TUE, MON, 2, "r4"), "CHECK constraint failed")
    rejected(lambda: raw_request(conn, 2, 1, "2025-01-06", "2025-01-06", 2, "r5"), "before the employee's start date")
    req = h.request_leave(conn, 2, "annual", MON, MON, "req")["request_id"]
    rejected(lambda: raw_request(conn, 2, 2, MON, TUE, 4, "r6"), "overlaps another pending or approved request")
    rejected(lambda: conn.execute("UPDATE hr_leave_requests SET status = 'approved' WHERE id = ?", (req,)),
             "leave request status must match its ledger")
    rejected(lambda: conn.execute("UPDATE hr_leave_requests SET end_date = ? WHERE id = ?", (TUE, req)),
             "only a leave request's status changes")
    rejected(lambda: conn.execute("DELETE FROM hr_leave_requests"), "rows are kept")
    rejected(lambda: raw_ledger(conn, 2, 1, "approve", -1, "r7", request_id=req), "does not match its leave request")
    rejected(lambda: raw_ledger(conn, 1, 1, "approve", -2, "r8", request_id=req), "leave balance would go negative")
    h.accrue_annual_leave(conn, 1, "annual", YEAR, "acc:1")                   # now employee 1 has a balance
    rejected(lambda: raw_ledger(conn, 1, 1, "approve", -2, "r8", request_id=req), "does not match its leave request")
    rejected(lambda: raw_ledger(conn, 2, 1, "request", 0, "r9", request_id=req), "not in a state for this step")
    rejected(lambda: raw_ledger(conn, 2, 1, "cancel", 2, "r10", request_id=req), "does not match its leave request")
    raw_ledger(conn, 2, 1, "approve", -2, "r11", request_id=req)              # the right amount: status follows
    assert h.leave_request(conn, req)["status"] == "approved"
    rejected(lambda: raw_ledger(conn, 2, 1, "reject", 0, "r12", request_id=req), "not in a state for this step")
    # The ledger rule fires first; "status can't move that way" is the backstop behind it (README).
    rejected(lambda: conn.execute("UPDATE hr_leave_requests SET status = 'pending' WHERE id = ?", (req,)),
             "leave request status must match its ledger")
    rejected(lambda: raw_ledger(conn, 2, 1, "cancel", 0, "r13", request_id=req), "does not match its leave request")
    raw_ledger(conn, 2, 1, "cancel", 2, "r14", request_id=req)
    assert h.leave_request(conn, req)["status"] == "cancelled" and bal(conn) == (2, 0, 2)
    rejected(lambda: raw_ledger(conn, 2, 1, "cancel", 0, "r15", request_id=req), "not in a state for this step")


def test_a_raw_request_without_its_request_entry_cannot_be_decided():
    conn, _ = shop("raw_orphan")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    rid = raw_request(conn, 2, 1, MON, MON, 2, "orphan").lastrowid
    rejected(lambda: raw_ledger(conn, 2, 1, "approve", -2, "x1", request_id=rid), "not in a state for this step")
    rejected(lambda: raw_ledger(conn, 2, 1, "cancel", 0, "x2", request_id=rid), "not in a state for this step")


def test_raw_every_column_rule_on_leave_tables():
    """Raw: each bad value in a leave type, request or ledger row is refused by its own CHECK or trigger."""
    conn, _ = shop("raw_leave_columns")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    good = {"code": "'x'", "name": "'X'", "paid": "1", "tracks_balance": "1"}
    for column, value, message in (("code", "'Bad Code'", "code <> '' AND code = lower(code)"),
                                   ("name", "''", "name <> ''"), ("paid", "2", "paid IN (0, 1)"),
                                   ("tracks_balance", "2", "tracks_balance IN (0, 1)")):
        v = good | {column: value}
        rejected(lambda: conn.execute(f"""INSERT INTO hr_leave_types (code, name, paid, tracks_balance,
                     annual_allowance_half_days, idempotency_key)
                     VALUES ({v['code']}, {v['name']}, {v['paid']}, {v['tracks_balance']}, 0, 'raw')"""),
                 f"CHECK constraint failed: {message}")
    rejected(lambda: conn.execute("DELETE FROM hr_leave_policy_changes"), "hr_leave_policy_changes rows are immutable")
    for args, message in (((2, 1, "2026-02-30", "2026-03-02", 2, "r"), "start_date IS date(start_date)"),
                          ((2, 1, MON, "2026-11-31", 2, "r"), "end_date IS date(end_date)"),
                          ((2, 1, MON, MON, 0, "r"), "half_days > 0"),
                          ((2, 1, TUE, MON, 1, "r"), "end_date >= start_date")):
        rejected(lambda: raw_request(conn, *args), f"CHECK constraint failed: {message}")
    rejected(lambda: conn.execute("""INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date, end_date,
                                     half_days, note, idempotency_key) VALUES (2, 1, ?, ?, 2, '', 'r')""", (MON, MON)),
             "CHECK constraint failed: note <> ''")
    ledger = """INSERT INTO hr_leave_ledger (employee_id, leave_type_id, entry_type, half_days, reason, as_of, actor,
                                             idempotency_key) VALUES (2, 1, 'adjustment', 1, 'x', ?, ?, 'r')"""
    rejected(lambda: conn.execute(ledger, ("2026-02-30", None)), "CHECK constraint failed: as_of IS date(as_of)")
    rejected(lambda: conn.execute(ledger, (None, "")), "CHECK constraint failed: actor <> ''")
    rejected(lambda: raw_ledger(conn, 2, 1, "bonus", 4, "r"), "CHECK constraint failed: entry_type IN ('accrual'")
    rejected(lambda: raw_ledger(conn, 2, 1, "accrual", 4, "r", accrual_year=1800),
             "CHECK constraint failed: accrual_year BETWEEN 1900 AND 9999")
    rejected(lambda: raw_ledger(conn, 2, 1, "approve", -2, "r"),
             "CHECK constraint failed: (entry_type IN ('accrual', 'adjustment')) = (request_id IS NULL)")
    rid = h.request_leave(conn, 2, "annual", MON, MON, "req")["request_id"]
    for entry_type, half_days in (("reject", 2), ("approve", 2), ("cancel", -2), ("accrual", 4)):
        rejected(lambda: raw_ledger(conn, 2, 1, entry_type, half_days, "r", request_id=rid, accrual_year=2027
                                    if entry_type == "accrual" else None), "ledger amount does not match its leave request")


def test_rows_are_kept_even_on_a_connection_without_foreign_keys():
    """Raw: a plain sqlite3 connection has foreign keys off, so only the no-delete triggers stop these."""
    conn, path = shop("raw_no_fk")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    h.request_leave(conn, 2, "annual", MON, MON, "req")
    plain = sqlite3.connect(path)
    try:
        assert plain.execute("PRAGMA foreign_keys").fetchone() == (0,)
        for table, message in (("hr_employees", "rows are kept"), ("hr_leave_types", "rows are kept"),
                               ("hr_leave_requests", "rows are kept"), ("hr_leave_ledger", "rows are immutable"),
                               ("hr_employment_events", "rows are immutable"), ("hr_commands", "rows are immutable"),
                               ("hr_leave_policy_changes", "rows are immutable")):
            rejected(lambda: plain.execute(f"DELETE FROM {table}"), f"{table} {message}")
    finally:
        plain.close()



def test_huge_amounts_and_ids_are_bad_input_not_a_crash():
    """Regression: 1e20 days or an id past 2**63 used to reach sqlite3 and raise OverflowError."""
    conn, _ = shop("huge_numbers")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    for days in (1e20, -1e20, 1e308, 10 ** 19, h.MAX_LEAVE_DAYS + 0.5):
        rejected(lambda: h.adjust_leave(conn, 2, "annual", days, "typo", f"adj:{days}"), "at most", ValueError)
    rejected(lambda: h.create_leave_type(conn, "big", "Big", "lt:big", annual_allowance_days=1e300), "at most",
             ValueError)
    rejected(lambda: h.set_leave_allowance(conn, "annual", 1e300, "typo", "pol:big"), "at most", ValueError)
    for big in (2 ** 63, 2 ** 70):
        rejected(lambda: h.approve_leave(conn, big, f"ap:{big}"), "positive integer", ValueError)
        rejected(lambda: h.request_leave(conn, big, "annual", MON, MON, f"rq:{big}"), "positive integer", ValueError)
        rejected(lambda: h.add_employee(conn, f"hire:{big}", employee_number="E9", first_name="A", job_title="B",
                                        start_date="2025-01-06", manager_id=big), "positive integer", ValueError)
    assert h.adjust_leave(conn, 2, "annual", h.MAX_LEAVE_DAYS, "the cap itself is fine", "adj:max")["days"] == \
        h.MAX_LEAVE_DAYS
    assert bal(conn) == (20 + h.MAX_LEAVE_DAYS, 0, 20 + h.MAX_LEAVE_DAYS)


def test_termination_derived_keys_never_collide_with_other_commands():
    """Regression: termination writes key/report:N and key/cancel:N into the event and ledger tables. Reusing one
    of them, before or after, used to fail with a raw UNIQUE IntegrityError; now it's an IdempotencyConflict and
    nothing is written."""
    conn, _ = shop("derived_keys")
    hire(conn, "E003", first_name="Lee", start_date="2025-03-03")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    req = h.request_leave(conn, 2, "annual", MON, MON, "req")
    # Before: a command already used the key termination would derive for Sam's pending request.
    h.adjust_leave(conn, 2, "annual", 1, "bonus", f"T/cancel:{req['request_id']}")
    before = dump(conn)
    rejected(lambda: h.terminate_employee(conn, 2, "2026-12-31", "left", "T"), "derived from 'T'",
             h.IdempotencyConflict)
    # Before: the key derived for a reassigned report.
    assert dump(conn) == before
    h.update_employee(conn, 3, {"job_title": "Helper"}, "2026-01-05", "U/report:2")
    before = dump(conn)
    rejected(lambda: h.terminate_employee(conn, 1, "2026-12-31", "sold", "U", reassign_reports_to=3),
             "derived from 'U'", h.IdempotencyConflict)
    assert dump(conn) == before
    assert h.employee(conn, 2)["status"] == "active" and h.employee(conn, 1)["status"] == "active"
    # After: termination took V/report:2 and V/cancel:N; a later command can't take them.
    h.terminate_employee(conn, 1, "2026-12-31", "sold", "V", reassign_reports_to=3)
    h.terminate_employee(conn, 2, "2026-12-31", "left", "W")
    for used in ("V/report:2", f"W/cancel:{req['request_id']}"):
        rejected(lambda: h.adjust_leave(conn, 3, "annual", 1, "x", used), "inside an earlier command",
                 h.IdempotencyConflict)
    assert h.terminate_employee(conn, 2, "2026-12-31", "left", "W")["status"] == "terminated"   # replay still works


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
