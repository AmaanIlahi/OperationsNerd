"""
HR module: employee records, the employment-history ledger, idempotency and
the database rules. Tests marked "raw" write SQL directly, bypassing hr.py, so
whatever they see rejected is rejected by the database itself. Each test gets
its own throwaway database under a temp directory. Run from the repo root with
the venv (pytest also collects these):

    python -B modules/hr/tests/test_employees.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hr as h

TMP = tempfile.TemporaryDirectory(prefix="hr_employees_", ignore_cleanup_errors=True)
TABLES = ("hr_commands", "hr_employees", "hr_employment_events", "hr_leave_types", "hr_leave_policy_changes",
          "hr_leave_requests", "hr_leave_ledger")


def new_db(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = h.connect(path)
    h.init_db(conn)
    return conn, path


def dump(conn) -> list:
    """Every row of every HR table, to show that something wrote nothing."""
    return [(t,) + tuple(r) for t in TABLES for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]


def rejected(fn, expected: str, error=sqlite3.IntegrityError) -> None:
    try:
        fn()
    except error as e:
        assert expected in str(e), f"rejected for the wrong reason: {e!r}"
        return
    raise AssertionError(f"expected {error.__name__} ({expected!r}), but it was accepted")


def hire(conn, number, key=None, **fields):
    fields.setdefault("first_name", f"Person {number}")
    fields.setdefault("job_title", "Shop assistant")
    fields.setdefault("start_date", "2025-01-06")
    return h.add_employee(conn, f"hire:{number}" if key is None else key, employee_number=number, **fields)


def raw_event(conn, employee_id, version, event, snapshot, key, effective_date="2026-01-05"):
    return conn.execute("""INSERT INTO hr_employment_events (employee_id, version, event, effective_date, changes,
                                                             snapshot, idempotency_key)
                           VALUES (?, ?, ?, ?, '{}', ?, ?)""",
                        (employee_id, version, event, effective_date, json.dumps(snapshot), key))


def snapshot_of(conn, employee_id) -> dict:
    row = conn.execute("SELECT * FROM hr_employees WHERE id = ?", (employee_id,)).fetchone()
    return {k: row[k] for k in h._SNAPSHOT_FIELDS}


# ---------------------------------------------------------------- hiring and records

def test_hiring_creates_the_employee_and_a_hired_event():
    conn, _ = new_db("hire")
    boss = hire(conn, "E001", first_name="Maya", last_name="Rao", job_title="Owner", department="Front of shop",
                work_email="maya@toyshop.test", work_phone="+1 555 0100", work_location="Main St")
    assert boss["employee_id"] == 1 and boss["status"] == "active" and boss["version"] == 1
    assert boss["employment_type"] == "full_time" and boss["end_date"] is None
    emp = hire(conn, "E002", manager_id=1, employment_type="part_time")
    assert emp["manager_id"] == 1 and emp["manager_name"] == "Maya Rao"
    assert h.employee(conn, 1)["direct_reports"] == 1
    history = h.employment_history(conn, 2)
    assert [(e["version"], e["event"], e["effective_date"]) for e in history] == [(1, "hired", "2025-01-06")]
    assert history[0]["changes"]["manager_id"] == [None, 1]
    assert [e["employee_number"] for e in h.employees(conn)] == ["E001", "E002"]
    assert [e["employee_number"] for e in h.employees(conn, department="Front of shop")] == ["E001"]


def test_required_fields_and_bad_values_are_rejected_before_anything_is_written():
    conn, _ = new_db("bad_fields")
    hire(conn, "E001")
    before = dump(conn)
    rejected(lambda: h.add_employee(conn, "k", first_name="A", job_title="B", start_date="2025-01-01"),
             "employee_number is required", ValueError)
    rejected(lambda: hire(conn, "E9", "k", first_name=""), "first_name must be", ValueError)
    rejected(lambda: hire(conn, "E9", "k", job_title=None), "job_title is required", ValueError)
    rejected(lambda: hire(conn, "E9", "k", start_date="2025-02-30"), "start_date must be a date", ValueError)
    rejected(lambda: hire(conn, "E9", "k", employment_type="gig"), "employment_type must be one of", ValueError)
    rejected(lambda: hire(conn, "E9", "k", work_email="nope"), "work_email must look like", ValueError)
    rejected(lambda: hire(conn, "E9", "k", manager_id=0), "manager_id must be", ValueError)
    rejected(lambda: hire(conn, "E9", "k", salary=50000), "unknown employee fields: salary", ValueError)
    rejected(lambda: hire(conn, "E9", ""), "idempotency_key must be", ValueError)
    assert dump(conn) == before
    assert not conn.in_transaction


def test_no_sensitive_fields_exist_anywhere():
    """Payroll and sensitive data are deferred (README): no column, field or API body may carry them."""
    conn, _ = new_db("no_sensitive")
    banned = {"salary", "wage", "wages", "pay", "payroll", "rate", "tax", "ssn", "sin", "national", "bank", "iban",
              "account", "routing", "dob", "birth", "birthday", "passport", "address", "medical", "health", "gender"}
    columns = [r["name"] for t in TABLES for r in conn.execute(f"PRAGMA table_info({t})")]
    for name in columns + list(h.EMPLOYEE_FIELDS):
        assert not banned & set(name.split("_")), name


def test_employee_numbers_and_work_emails_are_unique():
    conn, _ = new_db("unique")
    hire(conn, "E001", work_email="a@shop.test")
    rejected(lambda: hire(conn, "E001", "k2"), "already employee 1", h.InvalidState)
    rejected(lambda: hire(conn, "E002", "k3", work_email="A@SHOP.test"), "already employee 1's", h.InvalidState)
    hire(conn, "E002", work_email="b@shop.test")
    rejected(lambda: h.update_employee(conn, 2, {"work_email": "a@shop.test"}, "2026-01-05", "k4"),
             "already employee 1's", h.InvalidState)


def test_manager_must_be_active_and_loops_are_refused():
    conn, _ = new_db("managers")
    hire(conn, "E001")
    hire(conn, "E002", manager_id=1)
    hire(conn, "E003", manager_id=2)
    rejected(lambda: hire(conn, "E004", manager_id=99), "no manager 99", h.NotFound)
    rejected(lambda: h.update_employee(conn, 1, {"manager_id": 3}, "2026-01-05", "loop"), "would be a loop",
             ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"manager_id": 2}, "2026-01-05", "self"), "their own manager",
             ValueError)
    h.update_employee(conn, 3, {"manager_id": 1}, "2026-01-05", "move")
    h.terminate_employee(conn, 3, "2026-02-01", "moved away", "leave3")
    rejected(lambda: hire(conn, "E005", manager_id=3), "manager 3 is terminated", h.InvalidState)


def test_update_records_old_and_new_values_and_bumps_the_version():
    conn, _ = new_db("update")
    hire(conn, "E001")
    hire(conn, "E002", department="Stockroom")
    after = h.update_employee(conn, 2, {"job_title": "Supervisor", "department": None, "manager_id": 1},
                              "2026-03-02", "promo", reason="promotion")
    assert after["job_title"] == "Supervisor" and after["department"] is None and after["version"] == 2
    event = h.employment_history(conn, 2)[-1]
    assert event["event"] == "updated" and event["reason"] == "promotion" and event["effective_date"] == "2026-03-02"
    assert event["changes"] == {"job_title": ["Shop assistant", "Supervisor"], "department": ["Stockroom", None],
                                "manager_id": [None, 1]}
    before = dump(conn)
    rejected(lambda: h.update_employee(conn, 2, {"job_title": "Supervisor"}, "2026-03-03", "same"),
             "nothing changes", ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"start_date": "2020-01-01"}, "2026-03-03", "k"),
             "can't be updated: start_date", ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"status": "terminated"}, "2026-03-03", "k"),
             "can't be updated: status", ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"first_name": None}, "2026-03-03", "k"), "first_name must be",
             ValueError)
    rejected(lambda: h.update_employee(conn, 2, {"job_title": "X"}, "2024-01-01", "k"), "before the start date",
             ValueError)
    rejected(lambda: h.update_employee(conn, 9, {"job_title": "X"}, "2026-03-03", "k"), "no employee 9", h.NotFound)
    assert dump(conn) == before


# ---------------------------------------------------------------- termination

def test_termination_freezes_the_employee():
    conn, _ = new_db("terminate")
    hire(conn, "E001")
    out = h.terminate_employee(conn, 1, "2026-06-30", "resigned", "term")
    assert out["status"] == "terminated" and out["end_date"] == "2026-06-30" and out["termination_reason"] == "resigned"
    assert out["version"] == 2 and out["reassigned_reports"] == [] and out["cancelled_leave"] == []
    assert h.employment_history(conn, 1)[-1]["event"] == "terminated"
    assert [e["employee_number"] for e in h.employees(conn, status="terminated")] == ["E001"]
    assert h.employees(conn, status="active") == []
    rejected(lambda: h.update_employee(conn, 1, {"job_title": "X"}, "2026-07-01", "k"), "is terminated",
             h.InvalidState)
    rejected(lambda: h.terminate_employee(conn, 1, "2026-07-01", "again", "k"), "is terminated", h.InvalidState)


def test_termination_needs_reports_reassigned_and_does_it_in_one_transaction():
    conn, _ = new_db("terminate_reports")
    hire(conn, "E001")                       # owner
    hire(conn, "E002", manager_id=1)         # manager
    hire(conn, "E003", manager_id=2)
    hire(conn, "E004", manager_id=2)
    before = dump(conn)
    rejected(lambda: h.terminate_employee(conn, 2, "2026-05-29", "left", "t"), "still manages [3, 4]", h.InvalidState)
    rejected(lambda: h.terminate_employee(conn, 2, "2026-05-29", "left", "t", reassign_reports_to=3),
             "reports to employee 2", ValueError)
    rejected(lambda: h.terminate_employee(conn, 2, "2026-05-29", "left", "t", reassign_reports_to=2),
             "can't reassign reports to the employee being terminated", ValueError)
    rejected(lambda: h.terminate_employee(conn, 2, "2024-01-01", "left", "t", reassign_reports_to=1),
             "before the start date", ValueError)
    assert dump(conn) == before
    out = h.terminate_employee(conn, 2, "2026-05-29", "left", "t", reassign_reports_to=1)
    assert out["reassigned_reports"] == [3, 4]
    for emp_id in (3, 4):
        assert h.employee(conn, emp_id)["manager_id"] == 1
        last = h.employment_history(conn, emp_id)[-1]
        assert last["changes"] == {"manager_id": [2, 1]} and last["reason"] == "manager 2 terminated"
    assert h.employee(conn, 1)["direct_reports"] == 2


# ---------------------------------------------------------------- idempotency

def test_replaying_a_command_with_its_key_returns_the_same_result_and_writes_nothing():
    conn, _ = new_db("replay")
    first = hire(conn, "E001", "hire-1")
    before = dump(conn)
    assert hire(conn, "E001", "hire-1") == first
    assert dump(conn) == before
    upd = h.update_employee(conn, 1, {"job_title": "Lead"}, "2026-01-05", "upd-1")
    before = dump(conn)
    assert h.update_employee(conn, 1, {"job_title": "Lead"}, "2026-01-05", "upd-1") == upd
    term = h.terminate_employee(conn, 1, "2026-02-27", "closed", "term-1")
    assert h.terminate_employee(conn, 1, "2026-02-27", "closed", "term-1") == term
    assert len(h.employment_history(conn, 1)) == 3


def test_a_key_reused_for_another_payload_or_command_is_a_conflict():
    conn, _ = new_db("conflict")
    hire(conn, "E001", "k1")
    before = dump(conn)
    rejected(lambda: hire(conn, "E002", "k1"), "already used for add_employee", h.IdempotencyConflict)
    rejected(lambda: h.update_employee(conn, 1, {"job_title": "X"}, "2026-01-05", "k1"),
             "already used for add_employee", h.IdempotencyConflict)
    rejected(lambda: h.create_leave_type(conn, "annual", "Annual", "k1"), "already used for add_employee",
             h.IdempotencyConflict)
    assert dump(conn) == before


def test_a_failed_command_leaves_its_key_free():
    conn, _ = new_db("failed_key")
    rejected(lambda: hire(conn, "E001", "k", manager_id=5), "no manager 5", h.NotFound)
    assert hire(conn, "E001", "k")["employee_id"] == 1


def test_two_writers_hiring_at_once_get_distinct_ids():
    _, path = new_db("concurrent_hire")
    errors, barrier = [], threading.Barrier(8)

    def worker(n):
        conn = h.connect(path)
        try:
            barrier.wait()
            hire(conn, f"E{n:03d}")
        except Exception as e:                                                   # pragma: no cover
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    conn = h.connect(path)
    assert sorted(e["employee_id"] for e in h.employees(conn)) == list(range(1, 9))


# ---------------------------------------------------------------- raw: the database rules

def test_raw_employee_rows_can_only_come_from_an_event():
    conn, _ = new_db("raw_employee")
    hire(conn, "E001")
    rejected(lambda: conn.execute("""INSERT INTO hr_employees (employee_number, first_name, job_title, employment_type,
                                                              start_date, status, version, idempotency_key)
                                     VALUES ('X', 'Eve', 'Spy', 'full_time', '2025-01-01', 'active', 1, 'x')"""),
             "employee rows are written only by an employment event")
    rejected(lambda: conn.execute("UPDATE hr_employees SET job_title = 'CEO' WHERE id = 1"),
             "each change moves the employee version up by one")
    rejected(lambda: conn.execute("UPDATE hr_employees SET job_title = 'CEO', version = 2 WHERE id = 1"),
             "employee rows are written only by an employment event")
    rejected(lambda: conn.execute("DELETE FROM hr_employees WHERE id = 1"), "hr_employees rows are kept")


def test_raw_events_are_immutable_and_must_come_in_order():
    conn, _ = new_db("raw_events")
    hire(conn, "E001")
    rejected(lambda: conn.execute("UPDATE hr_employment_events SET reason = 'x'"), "rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM hr_employment_events"), "rows are immutable")
    rejected(lambda: conn.execute("UPDATE hr_commands SET result = '{}'"), "hr_commands rows are immutable")
    rejected(lambda: conn.execute("DELETE FROM hr_commands"), "hr_commands rows are immutable")
    snap = snapshot_of(conn, 1) | {"job_title": "Boss"}
    rejected(lambda: raw_event(conn, 1, 3, "updated", snap, "r1"), "must follow the employee's current version")
    rejected(lambda: raw_event(conn, 2, 1, "updated", snap | {"employee_number": "E2"}, "r2"), "CHECK constraint failed")
    rejected(lambda: raw_event(conn, 1, 2, "hired", snap, "r3"), "CHECK constraint failed")
    raw_event(conn, 1, 2, "updated", snap, "r4")                       # in order: projected onto the row
    assert h.employee(conn, 1)["job_title"] == "Boss" and h.employee(conn, 1)["version"] == 2


def test_raw_event_rules_on_termination_and_fixed_columns():
    conn, _ = new_db("raw_rules")
    hire(conn, "E001")
    hire(conn, "E002", manager_id=1)
    hire(conn, "E003")                                                   # nobody reports to them
    term = {"status": "terminated", "end_date": "2026-01-30", "termination_reason": "x"}
    rejected(lambda: raw_event(conn, 3, 2, "updated", snapshot_of(conn, 3) | term, "r1", "2026-01-30"),
             "CHECK constraint failed: (event = 'terminated') = (json_extract(snapshot, '$.status') = 'terminated')")
    rejected(lambda: raw_event(conn, 3, 2, "terminated", snapshot_of(conn, 3) | term, "r2", "2026-01-29"),
             "CHECK constraint failed: event <> 'terminated' OR json_extract(snapshot, '$.end_date') = effective_date")
    rejected(lambda: raw_event(conn, 1, 2, "terminated", snapshot_of(conn, 1) | term, "r3", "2026-01-30"),
             "employee still has active direct reports")
    rejected(lambda: raw_event(conn, 2, 2, "terminated", snapshot_of(conn, 2) | {"status": "terminated",
                                                                           "end_date": "2026-01-30"}, "r4", "2026-01-30"),
             "CHECK constraint failed")
    rejected(lambda: raw_event(conn, 2, 2, "updated", snapshot_of(conn, 2) | {"manager_id": 2}, "r5"),
             "manager must be an active employee")
    rejected(lambda: raw_event(conn, 2, 2, "updated", snapshot_of(conn, 2) | {"manager_id": 42}, "r6"),
             "manager must be an active employee")
    for fields in ({"start_date": "2020-01-06"}, {"employee_number": "E777"}):    # even through an event
        rejected(lambda: raw_event(conn, 2, 2, "updated", snapshot_of(conn, 2) | fields, "r-fixed"),
                 "employee id, number and start date never change")
    rejected(lambda: conn.execute("UPDATE hr_employees SET start_date = '2020-01-01', version = version + 1"),
             "employee id, number and start date never change")
    rejected(lambda: raw_event(conn, 2, 2, "updated", snapshot_of(conn, 2) | {"employment_type": "gig"}, "r7"),
             "CHECK constraint failed")
    h.update_employee(conn, 2, {"manager_id": None}, "2026-01-05", "detach")
    raw_event(conn, 2, 3, "terminated", snapshot_of(conn, 2) | {"status": "terminated", "end_date": "2026-01-30",
                                                                "termination_reason": "x"}, "r8", "2026-01-30")
    rejected(lambda: raw_event(conn, 2, 4, "updated", snapshot_of(conn, 2) | {"status": "active", "end_date": None,
                                                                    "termination_reason": None}, "r9",
                                     "2026-02-02"),
             "employee is terminated")


def test_a_raw_hire_needs_a_matching_new_employee():
    conn, _ = new_db("raw_hire")
    snap = {k: None for k in h._SNAPSHOT_FIELDS} | {"employee_number": "E1", "first_name": "A", "job_title": "B",
                                                     "employment_type": "full_time", "start_date": "2025-01-01",
                                                     "status": "active"}
    rejected(lambda: raw_event(conn, 1, 1, "hired", snap | {"status": "terminated", "end_date": "2025-02-01",
                                                            "termination_reason": "x"}, "r1"), "CHECK constraint failed")
    rejected(lambda: raw_event(conn, 1, 1, "hired", snap | {"start_date": "2025-13-01"}, "r2"),
             "CHECK constraint failed")
    raw_event(conn, 1, 1, "hired", snap, "r3")
    assert h.employee(conn, 1)["employee_number"] == "E1"


def test_raw_every_column_rule_on_employees_and_events():
    """Raw: each bad value, written through an event, is refused by its own CHECK, trigger or index."""
    conn, _ = new_db("raw_columns")
    hire(conn, "E001", work_email="maya@toyshop.test")
    hire(conn, "E002", key="h2", manager_id=1)
    hire(conn, "E003")
    h.terminate_employee(conn, 3, "2026-01-30", "left", "t3")
    new = {k: None for k in h._SNAPSHOT_FIELDS} | {"employee_number": "E9", "first_name": "A", "job_title": "B",
                                                    "employment_type": "full_time", "start_date": "2025-01-01",
                                                    "status": "active"}
    hires = [({f: ""}, f"CHECK constraint failed: {f} <> ''")
             for f in ("employee_number", "first_name", "last_name", "preferred_name", "work_phone", "job_title",
                       "department", "work_location")]
    hires += [({"work_email": "nope"}, "CHECK constraint failed: work_email LIKE '_%@_%'"),
              ({"work_email": "MAYA@toyshop.test"}, "UNIQUE constraint failed: index 'hr_employees_work_email'"),
              ({"status": "retired"}, "an employee starts active at version 1"),
              ({"manager_id": 3}, "manager must be an active employee"),          # 3 has left
              ({"manager_id": 99}, "manager must be an active employee")]
    for fields, message in hires:
        rejected(lambda: raw_event(conn, 4, 1, "hired", new | fields, "raw"), message)
    current = snapshot_of(conn, 2)
    left = {"status": "terminated", "end_date": "2026-01-05", "termination_reason": "left"}
    changes = [("updated", {"status": "retired"}, "2026-01-05", "CHECK constraint failed: status IN ('active', 'terminated')"),
               ("updated", {"end_date": "2026-01-05"}, "2026-01-05",
                "CHECK constraint failed: (status = 'terminated') = (end_date IS NOT NULL)"),
               ("terminated", left | {"termination_reason": ""}, "2026-01-05",
                "CHECK constraint failed: termination_reason <> ''"),
               # Migration 0003: an event dated before the hire is now refused first by the date-order trigger.
               ("terminated", left | {"end_date": "2024-01-05"}, "2024-01-05",
                "an employment event can't be dated before the previous one"),
               ("promoted", {}, "2026-01-05",
                "CHECK constraint failed: event IN ('hired', 'updated', 'terminated', 'rehired')"),
               ("updated", {}, "2026-02-30", "CHECK constraint failed: effective_date IS date(effective_date)")]
    for event, fields, effective, message in changes:
        rejected(lambda: raw_event(conn, 2, 2, event, current | fields, "raw", effective_date=effective), message)
    insert = """INSERT INTO hr_employment_events (employee_id, version, event, effective_date, changes, snapshot, reason,
                                                  idempotency_key) VALUES (2, 2, 'updated', '2026-01-05', ?, ?, ?, 'raw')"""
    for args, message in (
            (("[]", json.dumps(current), None), "json_valid(changes) AND json_type(changes) = 'object'"),
            (("{}", "[]", None), "json_valid(snapshot) AND json_type(snapshot) = 'object'"),
            (("{}", json.dumps(current), ""), "CHECK constraint failed: reason <> ''")):
        rejected(lambda: conn.execute(insert, args), message)
    rejected(lambda: conn.execute("INSERT INTO hr_command_names (name) VALUES ('')"), "CHECK constraint failed: name <> ''")


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
