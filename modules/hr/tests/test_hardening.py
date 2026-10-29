"""
HR module: regression tests for the second hardening pass. These cover:
- the cancel clock
- leave at termination
- rehiring
- clean identity fields
- text length limits
- effective dates in order
- reads with bad ids
- morning/afternoon halves
- the schema integrity check

Tests marked "raw" write SQL directly, bypassing hr.py. Run from the repo root
with the venv (pytest also collects these):

    python -B modules/hr/tests/test_hardening.py
"""

import datetime
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr as h
from test_employees import dump, hire, new_db, raw_event, rejected, snapshot_of
from test_leave import MON, TUE, WED, THU, FRI, SAT, TODAY, YEAR, bal, raw_ledger, raw_request, shop


# ---------------------------------------------------------------- cancelling: the clock

def test_approved_leave_is_cancelled_only_before_it_starts():
    conn, _ = shop("cancel_clock")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    a = h.request_leave(conn, 2, "annual", MON, WED, "req:a")
    h.approve_leave(conn, a["request_id"], "ok:a")
    before = dump(conn)
    for today in (MON, TUE, "2026-11-30"):                          # its first day, during it, after it
        rejected(lambda: h.cancel_leave(conn, a["request_id"], f"cx:{today}", today=today),
                 f"started on {MON} (today is {today})", h.InvalidState)
    assert dump(conn) == before and bal(conn) == (17, 0, 17)
    rejected(lambda: h.cancel_leave(conn, a["request_id"], "cx", today="2026-13-01"), "today must be", ValueError)
    out = h.cancel_leave(conn, a["request_id"], "cx", today="2026-11-01")     # the day before: credited in full
    assert out["status"] == "cancelled" and bal(conn) == (20, 0, 20)
    # today isn't part of the command: replaying the key later returns the first answer.
    assert h.cancel_leave(conn, a["request_id"], "cx", today="2027-01-01") == out


def test_pending_leave_can_be_cancelled_any_day_and_today_defaults_to_the_clock():
    conn, _ = shop("cancel_pending")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    p = h.request_leave(conn, 2, "annual", MON, MON, "req:p")
    assert h.cancel_leave(conn, p["request_id"], "cx:p", today="2027-06-01")["status"] == "cancelled"
    far = (datetime.date.today() + datetime.timedelta(days=400))
    far -= datetime.timedelta(days=far.weekday())                              # a Monday, after the real today
    future = h.request_leave(conn, 2, "unpaid", far.isoformat(), far.isoformat(), "req:far")
    h.approve_leave(conn, future["request_id"], "ok:far")
    assert h.cancel_leave(conn, future["request_id"], "cx:far")["status"] == "cancelled"
    past = h.request_leave(conn, 2, "unpaid", "2025-03-03", "2025-03-03", "req:past")
    h.approve_leave(conn, past["request_id"], "ok:past")
    rejected(lambda: h.cancel_leave(conn, past["request_id"], "cx:past"), "approved leave is cancelled only before",
             h.InvalidState)


# ---------------------------------------------------------------- leave at termination

def test_termination_on_a_weekend_credits_leave_with_no_working_day_left_in_full():
    conn, _ = shop("term_weekend")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    r = h.request_leave(conn, 2, "annual", SAT, "2026-11-10", "req")          # Sat..Tue: Mon and Tue, 2 days
    h.approve_leave(conn, r["request_id"], "ok")
    assert bal(conn) == (18, 0, 18)
    out = h.terminate_employee(conn, 2, SAT, "left", "term")
    assert out["shortened_leave"] == [{"request_id": 1, "replaced_by": None, "kept_days": 0, "credited_days": 2}]
    assert bal(conn) == (20, 0, 20)


def test_termination_charges_spanning_leave_up_to_the_end_date_only():
    conn, _ = shop("term_span")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    r = h.request_leave(conn, 2, "annual", MON, FRI, "req", note="trip")
    h.approve_leave(conn, r["request_id"], "ok")
    h.request_leave(conn, 2, "unpaid", "2026-11-09", "2026-11-09", "req:u")
    out = h.terminate_employee(conn, 2, WED, "left", "term")
    assert out["shortened_leave"] == [{"request_id": 1, "replaced_by": 3, "kept_days": 3, "credited_days": 2}]
    kept = h.leave_request(conn, 3)
    assert (kept["start_date"], kept["end_date"], kept["days"], kept["status"], kept["note"]) == (
        MON, WED, 3, "approved", "trip")
    assert bal(conn) == (17, 0, 17) and h.leave_balance(conn, 2, "annual")["taken"] == 3
    before = dump(conn)
    assert h.terminate_employee(conn, 2, WED, "left", "term") == out            # replay
    assert dump(conn) == before


def test_termination_refuses_a_key_whose_shorten_step_was_used():
    conn, _ = shop("term_shorten_key")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    r = h.request_leave(conn, 2, "annual", MON, FRI, "req")
    h.approve_leave(conn, r["request_id"], "ok")
    h.adjust_leave(conn, 2, "annual", 1, "x", "T/shorten:1/approve")
    before = dump(conn)
    rejected(lambda: h.terminate_employee(conn, 2, WED, "left", "T"), "derived from 'T'", h.IdempotencyConflict)
    assert dump(conn) == before


def test_raw_nothing_is_written_to_the_ledger_of_someone_who_has_left():
    """Raw: not even a cancel or a reject, so no credit can arrive after termination."""
    conn, _ = shop("term_raw_ledger")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    a = h.request_leave(conn, 2, "annual", "2026-10-05", "2026-10-06", "req")
    h.approve_leave(conn, a["request_id"], "ok")
    h.terminate_employee(conn, 2, "2026-10-30", "left", "term")
    for entry_type, half_days in (("cancel", 4), ("reject", 0), ("approve", -4), ("request", 0)):
        rejected(lambda: raw_ledger(conn, 2, 1, entry_type, half_days, f"raw:{entry_type}", request_id=1),
                 "employee is not active")


# ---------------------------------------------------------------- rehire

def test_rehire_brings_back_the_same_record_with_its_history():
    conn, _ = shop("rehire")
    h.update_employee(conn, 2, {"work_email": "sam@toyshop.test"}, "2025-04-01", "email")
    h.accrue_annual_leave(conn, 2, "annual", 2025, "acc25")
    h.terminate_employee(conn, 2, "2025-12-31", "left", "term")
    out = h.rehire_employee(conn, 2, "2026-03-02", "back", changes={"job_title": "Lead"}, reason="came back")
    assert (out["employee_id"], out["employee_number"], out["work_email"], out["status"], out["end_date"],
            out["termination_reason"], out["job_title"], out["manager_id"]) == (
        2, "E002", "sam@toyshop.test", "active", None, None, "Lead", 1)
    assert out["start_date"] == "2025-03-03" and out["employment_start_date"] == "2026-03-02"
    assert [e["event"] for e in h.employment_history(conn, 2)] == ["hired", "updated", "terminated", "rehired"]
    assert h.employment_history(conn, 2)[-1]["changes"]["status"] == ["terminated", "active"]
    assert h.rehire_employee(conn, 2, "2026-03-02", "back", changes={"job_title": "Lead"}, reason="came back") == out
    # Leave is requested and accrued from the rehire date; the balance carried over.
    assert bal(conn)[0] == h.leave_balance(conn, 2, "annual")["accrued"]
    rejected(lambda: h.request_leave(conn, 2, "annual", "2026-02-23", "2026-02-23", "early"),
             "before the employee's start date 2026-03-02", ValueError)
    acc = h.accrue_annual_leave(conn, 2, "annual", 2026, "acc26")
    assert acc["prorated"] and acc["days"] == 16.5                              # 305 of 365 days of 20
    h.request_leave(conn, 2, "annual", "2026-03-02", "2026-03-02", "first-day")
    h.terminate_employee(conn, 2, "2026-06-30", "left again", "term2")
    assert h.rehire_employee(conn, 2, "2026-09-01", "back2")["employment_start_date"] == "2026-09-01"


def test_rehire_rules():
    conn, _ = shop("rehire_rules")
    hire(conn, "E003", first_name="Ana", manager_id=2, start_date="2025-03-03")
    rejected(lambda: h.rehire_employee(conn, 2, "2026-01-05", "k"), "only a terminated employee", h.InvalidState)
    h.terminate_employee(conn, 3, "2025-12-31", "left", "t3")
    for date in ("2025-12-31", "2025-06-02"):
        rejected(lambda: h.rehire_employee(conn, 3, date, "k"), "must be after the end date", ValueError)
    h.update_employee(conn, 2, {"manager_id": None}, "2026-01-05", "detach")
    h.terminate_employee(conn, 2, "2026-01-30", "left", "t2")                    # Ana's old manager left
    rejected(lambda: h.rehire_employee(conn, 3, "2026-02-02", "k"), "pass changes={'manager_id'", h.InvalidState)
    hire(conn, "E004", first_name="Bo", work_email="ana@toyshop.test", start_date="2026-01-05")
    rejected(lambda: h.rehire_employee(conn, 3, "2026-02-02", "k", changes={"manager_id": 1,
                                                                            "work_email": "ANA@toyshop.test"}),
             "is already employee 4's", h.InvalidState)
    rejected(lambda: h.rehire_employee(conn, 3, "2026-02-02", "k", changes={"start_date": "2026-02-02"}),
             "can't be set on rehire", ValueError)
    rejected(lambda: h.rehire_employee(conn, 9, "2026-02-02", "k"), "no employee 9", h.NotFound)
    assert h.rehire_employee(conn, 3, "2026-02-02", "k", changes={"manager_id": 1})["manager_id"] == 1


def test_leave_from_an_earlier_employment_is_not_cancelled_after_a_rehire():
    conn, _ = shop("rehire_old_leave")
    h.accrue_annual_leave(conn, 2, "annual", 2025, "acc")
    old = h.request_leave(conn, 2, "annual", "2025-11-03", "2025-11-04", "old")
    h.approve_leave(conn, old["request_id"], "ok:old")
    h.terminate_employee(conn, 2, "2025-12-31", "left", "term")
    h.rehire_employee(conn, 2, "2026-03-02", "back")
    rejected(lambda: h.cancel_leave(conn, old["request_id"], "cx", today="2025-01-01"), "from an earlier employment",
             h.InvalidState)


def test_raw_rehire_rules():
    conn, _ = new_db("raw_rehire")
    hire(conn, "E001")
    hire(conn, "E002", manager_id=1)
    hire(conn, "E003", manager_id=1)
    back = {"status": "active", "end_date": None, "termination_reason": None}
    h.terminate_employee(conn, 3, "2026-01-23", "left", "t3")            # still lists manager 1
    rejected(lambda: raw_event(conn, 2, 2, "rehired", snapshot_of(conn, 2), "r1", "2026-03-02"),
             "a rehire follows a termination")
    h.update_employee(conn, 2, {"manager_id": None}, "2026-01-05", "detach")
    h.terminate_employee(conn, 2, "2026-01-30", "left", "t2")
    rejected(lambda: raw_event(conn, 2, 4, "rehired", snapshot_of(conn, 2) | back, "r2", "2026-01-30"),
             "a rehire follows a termination and starts after its end date")
    rejected(lambda: raw_event(conn, 2, 4, "updated", snapshot_of(conn, 2) | back, "r3", "2026-02-02"),
             "employee is terminated")
    rejected(lambda: raw_event(conn, 2, 4, "rehired", snapshot_of(conn, 2) | back | {"manager_id": 99}, "r4",
                               "2026-02-02"), "manager must be an active employee")
    h.terminate_employee(conn, 1, "2026-01-30", "closed", "t1")
    rejected(lambda: raw_event(conn, 2, 4, "rehired", snapshot_of(conn, 2) | back | {"manager_id": 1}, "r5",
                               "2026-02-02"), "manager must be an active employee")
    # Manager 1 is unchanged on 3's record, so only the rehire rule sees that they have left.
    rejected(lambda: raw_event(conn, 3, 3, "rehired", snapshot_of(conn, 3) | back, "r7", "2026-02-02"),
             "manager must be an active employee")
    raw_event(conn, 2, 4, "rehired", snapshot_of(conn, 2) | back, "r6", "2026-02-02")
    assert h.employee(conn, 2)["status"] == "active"
    # A later event's key can't be empty (the hire's key is also checked on hr_employees).
    rejected(lambda: raw_event(conn, 2, 5, "updated", snapshot_of(conn, 2) | {"job_title": "Lead"}, "",
                               "2026-02-03"), "CHECK constraint failed: idempotency_key <> ''")


# ---------------------------------------------------------------- clean identity fields

def test_numbers_names_and_emails_are_trimmed_and_normalised():
    conn, _ = new_db("clean")
    out = h.add_employee(conn, "h1", employee_number="  E001\t", first_name=" Jose\u0301 ", last_name="de  la   Cruz",
                         job_title=" Owner ", work_email="  Maya@ToyShop.TEST ", start_date="2025-01-06")
    assert (out["employee_number"], out["first_name"], out["last_name"], out["job_title"], out["work_email"]) == (
        "E001", "Jos\u00e9", "de la Cruz", "Owner", "maya@toyshop.test")
    # The same hire with different spacing is the same command (a replay), not a conflict.
    assert h.add_employee(conn, "h1", employee_number="E001", first_name="Jos\u00e9", last_name="de la Cruz",
                          job_title="Owner", work_email="maya@toyshop.test", start_date="2025-01-06") == out
    rejected(lambda: h.add_employee(conn, "h2", employee_number=" E001 ", first_name="B", job_title="C",
                                    start_date="2025-01-06"), "employee number 'E001' is already", h.InvalidState)
    rejected(lambda: h.add_employee(conn, "h3", employee_number="E002", first_name="B", job_title="C",
                                    work_email="MAYA@toyshop.test", start_date="2025-01-06"),
             "is already employee 1's", h.InvalidState)
    rejected(lambda: h.add_employee(conn, "h4", employee_number="   ", first_name="B", job_title="C",
                                    start_date="2025-01-06"), "employee_number must be", ValueError)
    upd = h.update_employee(conn, 1, {"work_email": " OWNER@toyshop.test", "department": "  Front   desk "},
                            "2025-02-03", "u")
    assert upd["work_email"] == "owner@toyshop.test" and upd["department"] == "Front desk"
    assert [e["employee_id"] for e in h.employees(conn, department=" Front desk")] == [1]
    rejected(lambda: h.update_employee(conn, 1, {"work_email": "Owner@ToyShop.test"}, "2025-02-03", "u2"),
             "nothing changes", ValueError)


def test_raw_new_numbers_and_emails_must_be_clean():
    conn, _ = new_db("raw_clean")
    new = {k: None for k in h._SNAPSHOT_FIELDS} | {"employee_number": "E9", "first_name": "A", "job_title": "B",
                                                    "employment_type": "full_time", "start_date": "2025-01-01",
                                                    "status": "active"}
    for fields in ({"employee_number": " E9"}, {"employee_number": "E9\n"}):
        rejected(lambda: raw_event(conn, 1, 1, "hired", new | fields, "r"), "must be trimmed")
    insert = """INSERT INTO hr_employment_events (employee_id, version, event, effective_date, changes, snapshot,
                                                  idempotency_key) VALUES (1, 1, 'hired', '2025-01-01', ?, ?, 'r')"""
    for email in ("A@shop.test", " a@shop.test"):
        snap = new | {"work_email": email}
        rejected(lambda: conn.execute(insert, (json.dumps({"work_email": [None, email]}), json.dumps(snap))),
                 "must be trimmed (and the email lowercase)")
    conn.execute(insert, (json.dumps({"work_email": [None, "a@shop.test"]}), json.dumps(new | {"work_email": "a@shop.test"})))


# ---------------------------------------------------------------- length limits

def test_every_text_input_has_a_length_limit():
    conn, _ = shop("limits")
    long, longer = "x" * (h.MAX_TEXT + 1), "x" * (h.MAX_LONG_TEXT + 1)
    hire_ok = dict(employee_number="E9", first_name="A", job_title="B", start_date="2025-01-06")
    for field in ("employee_number", "first_name", "last_name", "preferred_name", "work_phone", "job_title",
                  "department", "work_location"):
        rejected(lambda: h.add_employee(conn, "k", **(hire_ok | {field: long})), f"{field} must be at most 200",
                 ValueError)
    rejected(lambda: h.add_employee(conn, "k", **(hire_ok | {"work_email": "a@" + long})), "at most 200", ValueError)
    rejected(lambda: h.add_employee(conn, "k" * 201, **hire_ok), "idempotency_key must be at most 200", ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"job_title": "X"}, "2026-01-05", "k", reason=longer),
             "reason must be at most 2000", ValueError)
    rejected(lambda: h.create_leave_type(conn, "c" * 201, "x", "k"), "code must be at most", ValueError)
    rejected(lambda: h.create_leave_type(conn, "c", long, "k"), "name must be at most", ValueError)
    rejected(lambda: h.request_leave(conn, 2, "unpaid", MON, MON, "k", note=longer), "note must be at most",
             ValueError)
    rejected(lambda: h.reject_leave(conn, 1, longer, "k"), "reason must be at most", ValueError)
    rejected(lambda: h.approve_leave(conn, 1, "k", approver=long), "actor must be at most", ValueError)
    rejected(lambda: h.terminate_employee(conn, 2, "2026-01-05", longer, "k"), "reason must be at most", ValueError)
    # At the limit is fine (after trimming).
    out = h.add_employee(conn, "k", **(hire_ok | {"first_name": " " + "y" * h.MAX_TEXT + " "}))
    assert len(out["first_name"]) == h.MAX_TEXT
    h.request_leave(conn, 2, "unpaid", MON, MON, "k2", note="n" * h.MAX_LONG_TEXT)


# ---------------------------------------------------------------- effective dates in order

def test_employment_events_cannot_be_dated_before_the_previous_one():
    conn, _ = new_db("dates")
    hire(conn, "E001", start_date="2025-01-06")
    hire(conn, "E002", manager_id=1, start_date="2025-01-06")
    h.update_employee(conn, 2, {"job_title": "Lead"}, "2025-06-02", "u1")
    rejected(lambda: h.update_employee(conn, 2, {"job_title": "Boss"}, "2025-05-30", "u2"),
             "before employee 2's latest employment event (2025-06-02)", ValueError)
    h.update_employee(conn, 2, {"job_title": "Boss"}, "2025-06-02", "u3")                # same day is fine
    rejected(lambda: h.terminate_employee(conn, 2, "2025-05-30", "left", "t"), "latest employment event",
             ValueError)
    # A report whose history is dated after their manager's last day moves on its own latest date.
    hire(conn, "E003", start_date="2025-01-06")
    h.update_employee(conn, 2, {"department": "Shop"}, "2025-09-01", "u4")
    h.terminate_employee(conn, 1, "2025-08-29", "closed", "t1", reassign_reports_to=3)
    assert h.employment_history(conn, 2)[-1]["effective_date"] == "2025-09-01"
    assert h.employee(conn, 2)["manager_id"] == 3


def test_raw_events_must_be_dated_in_order():
    conn, _ = new_db("raw_dates")
    hire(conn, "E001", start_date="2025-01-06")
    raw_event(conn, 1, 2, "updated", snapshot_of(conn, 1) | {"job_title": "Lead"}, "r1", "2025-06-02")
    rejected(lambda: raw_event(conn, 1, 3, "updated", snapshot_of(conn, 1) | {"job_title": "X"}, "r2", "2025-06-01"),
             "can't be dated before the previous one")
    raw_event(conn, 1, 3, "updated", snapshot_of(conn, 1) | {"job_title": "X"}, "r3", "2025-06-02")


# ---------------------------------------------------------------- reads with bad ids

def test_reads_with_huge_or_odd_ids_are_not_found_or_value_errors():
    conn, _ = shop("ids")
    for bad in (2 ** 63, 2 ** 64 + 5, 10 ** 30, 0, -1):
        for read in (lambda: h.employee(conn, bad), lambda: h.employment_history(conn, bad),
                     lambda: h.leave_balance(conn, bad, "annual"), lambda: h.leave_balances(conn, bad),
                     lambda: h.leave_ledger(conn, bad), lambda: h.leave_request(conn, bad)):
            rejected(read, "no ", h.NotFound)
        assert h.leave_requests(conn, employee_id=bad) == []
    for odd in ("1", 1.0, True, None):
        for read in (lambda: h.employee(conn, odd), lambda: h.leave_request(conn, odd),
                     lambda: h.leave_balances(conn, odd), lambda: h.leave_requests(conn, employee_id=odd if odd
                                                                                  is not None else "x")):
            rejected(read, "must be an integer", ValueError)
    rejected(lambda: h.leave_requests(conn, status="weird"), "status must be", ValueError)
    rejected(lambda: h.leave_balances(conn, 9), "no employee 9", h.NotFound)


# ---------------------------------------------------------------- morning and afternoon

def test_morning_and_afternoon_of_one_day_are_separate_halves():
    conn, _ = shop("halves")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    am = h.request_leave(conn, 2, "annual", MON, MON, "am", half_day=True)
    assert (am["half"], am["days"]) == ("am", 0.5)
    pm = h.request_leave(conn, 2, "unpaid", MON, MON, "pm", half_day=True, half="pm")
    assert (pm["half"], pm["days"]) == ("pm", 0.5)
    rejected(lambda: h.request_leave(conn, 2, "annual", MON, MON, "am2", half_day=True, half="am"),
             "overlaps pending request 1", h.Overlap)
    rejected(lambda: h.request_leave(conn, 2, "annual", MON, MON, "full"), "overlaps pending request 1", h.Overlap)
    rejected(lambda: h.request_leave(conn, 2, "annual", TUE, TUE, "x", half="pm"), "is for a half_day request",
             ValueError)
    rejected(lambda: h.request_leave(conn, 2, "annual", TUE, TUE, "x", half_day=True, half="noon"),
             "half must be 'am' or 'pm'", ValueError)
    full = h.request_leave(conn, 2, "annual", TUE, TUE, "tue")
    assert full["half"] is None
    rejected(lambda: h.request_leave(conn, 2, "annual", TUE, TUE, "tue-pm", half_day=True, half="pm"),
             "overlaps pending request 3", h.Overlap)
    # A half-day request made before halves existed replays with the same key.
    assert h.request_leave(conn, 2, "annual", MON, MON, "am", half_day=True) == am
    assert h.request_leave(conn, 2, "annual", MON, MON, "am", half_day=True, half="am") == am
    rejected(lambda: h.request_leave(conn, 2, "annual", MON, MON, "am", half_day=True, half="pm"),
             "already used for request_leave", h.IdempotencyConflict)


def test_raw_halves_have_one_shape_and_a_whole_day_blocks_both():
    conn, _ = shop("raw_halves")
    h.accrue_annual_leave(conn, 2, "annual", YEAR, "acc")
    insert = """INSERT INTO hr_leave_requests (employee_id, leave_type_id, start_date, end_date, half_days,
                                               idempotency_key, half) VALUES (2, 2, ?, ?, ?, ?, ?)"""
    rejected(lambda: conn.execute(insert, (MON, TUE, 1, "r1", "am")), "one half-day on one date")
    rejected(lambda: conn.execute(insert, (MON, MON, 2, "r2", "pm")), "one half-day on one date")
    rejected(lambda: conn.execute(insert, (MON, MON, 1, "r3", "noon")), "CHECK constraint failed")
    conn.execute(insert, (MON, MON, 1, "r4", "am"))
    conn.execute(insert, (MON, MON, 1, "r5", "pm"))
    rejected(lambda: conn.execute(insert, (MON, MON, 1, "r6", "pm")), "overlaps another pending or approved")
    raw_request(conn, 2, 2, WED, WED, 1, "r7")                                       # an old-style half, no side
    for half in ("am", "pm"):
        rejected(lambda: conn.execute(insert, (WED, WED, 1, f"r8{half}", half)), "overlaps another pending")
    rejected(lambda: conn.execute("UPDATE hr_leave_requests SET half = 'pm' WHERE idempotency_key = 'r4'"),
             "only a leave request's status changes")


# ---------------------------------------------------------------- the schema integrity check

def test_a_tampered_schema_is_refused_at_startup():
    for name, tamper in [
        ("dropped", "DROP TRIGGER hr_leave_ledger_balance_not_negative"),
        ("changed", "DROP TRIGGER hr_leave_ledger_no_update; CREATE TRIGGER hr_leave_ledger_no_update BEFORE UPDATE "
                    "ON hr_leave_ledger WHEN 0 BEGIN SELECT RAISE(ABORT, 'x'); END"),
        ("extra trigger", "CREATE TRIGGER sneaky AFTER INSERT ON hr_leave_ledger BEGIN SELECT 1; END"),
        ("extra table", "CREATE TABLE hr_backdoor (id INTEGER)"),
        ("dropped index", "DROP INDEX hr_leave_ledger_one_accrual"),
    ]:
        conn, path = new_db(f"tamper_{name.replace(' ', '_')}")
        h.verify_schema(conn)
        conn.executescript(tamper)
        rejected(lambda: h.verify_schema(conn), "isn't what its migrations create", h.SchemaTampered)
        conn.close()
        c2 = h.connect(path)
        rejected(lambda: h.init_db(c2), "isn't what its migrations create", h.SchemaTampered)
        c2.close()
    conn, _ = new_db("untampered")
    conn.execute("CREATE TABLE stock_movements (id INTEGER PRIMARY KEY)")      # another module's tables are fine
    conn.execute("CREATE TRIGGER stock_t AFTER INSERT ON stock_movements BEGIN SELECT 1; END")
    h.verify_schema(conn)
    assert h.init_db(conn)["applied"] == []


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
