"""
Checks that the HR tests aren't vacuous: removes each database rule in turn (every
trigger and unique index in migrations/, and the CHECK constraints) from a temp copy
of modules/hr/, runs the tests against it, and reports which tests fail. Nothing in
the real folder is changed. Run from the repo root with the venv (a minute or two):

    python -B modules/hr/tests/mutation_check.py

Each removal ends up in one of three outcomes:
  caught     a test fails because a bad write was accepted or a command misbehaved
  redundant  tests only fail because another rule now rejects the write, with a
             different message: the rule is implied by the others
  survived   no test fails: the rule is untested, or shadowed by a rule that always
             fires first

A rule a later migration replaces (dropped and created again under the same name, or
a CHECK on a table that is rebuilt) is removed only where it is last defined, which
is the version a current database runs; tables are labelled by their final name.

Exits non-zero if the unchanged copy fails, or the redundant or survived sets differ
from EXPECTED_REDUNDANT and EXPECTED_SHADOWED. Only a test's first failure is
recorded, so a test that checks several rules lists the cases in the order that
lets each removal show up as caught. test_api.py is left out: it starts the whole app
from the real repo, and the router has no rules of its own.
"""

import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
WRONG_REASON = "rejected for the wrong reason"
JOBS = 4

# Implied by the other rules today: removing one still rejects the bad write, with another rule's message.
# Kept as backstops with a clearer message, in case a later migration loosens the rule that implies them.
EXPECTED_REDUNDANT = {
    "check hr_employment_events: ((event = 'hired') = (version = 1)",          # hire projection / FK
    "check hr_employment_events: (json_valid(snapshot) AND json_type(snapshot) = 'object'",  # NOT NULLs
    "check hr_leave_ledger: (entry_type IN ('accrual', 'adjustment', 'request', 'approve",    # request_id CHECK
    "check hr_leave_requests: (end_date >= start_date",                       # half_days <= 2 * days CHECK
    "trigger hr_employees_born_active",                                        # status CHECK + event CHECK
    "trigger hr_employees_version_step",                                       # matches_event
    "trigger hr_employment_events_in_order",                                   # version_step
    "trigger hr_leave_ledger_adjustment_within_available",                     # balance_not_negative
    "trigger hr_leave_policy_changes_tracked_only",                            # leave type CHECK
    "trigger hr_leave_requests_fixed_columns",                                 # status_flow
}

# Shadowed: another rule always fires first with the message the tests expect, so no test can tell these
# are gone. Each is a second line of defence behind the rule named.
EXPECTED_SHADOWED = {
    "check hr_leave_requests: (status IN ('pending', 'approved', 'rejected', 'cancelled')",  # born_pending, ledger
    "trigger hr_leave_requests_status_flow",                                   # status_matches_ledger
    "check hr_leave_ledger: (entry_type NOT IN ('request', 'reject') OR half_days = 0",     # request_amount
    "check hr_leave_ledger: (entry_type <> 'approve' OR half_days < 0",        # request_amount
    "check hr_leave_ledger: (entry_type <> 'cancel' OR half_days >= 0",        # request_amount
    "unique index hr_leave_ledger_one_step",                                   # request_step
    "check hr_employees: (end_date IS NULL OR end_date >= start_date",         # events dates_in_order
    "check hr_employment_events: (version >= 1",                               # events in_order
}

HARNESS = r'''
import sys
sys.path.insert(0, sys.argv[1] + "/tests")
import test_employees, test_hardening, test_leave, test_migrations, test_toy_shop_walkthrough
for t in (test_employees, test_hardening, test_leave, test_migrations, test_toy_shop_walkthrough):
    for name, fn in [(n, f) for n, f in vars(t).items() if n.startswith("test_") and callable(f)]:
        try:
            fn()
        except BaseException as e:
            print("FAIL", name, "|", type(e).__name__, (str(e).splitlines() or [""])[0][:120])
'''


# Command logic in hr.py that the database can't check: (label, file, old, new).
COMMAND_MUTATIONS = [
    ("hr.py: commands run outside a transaction", "hr.py",
     [('    conn.execute("BEGIN IMMEDIATE")\n    try:\n        row = ', '    try:\n        row = '),
      ('        conn.execute("COMMIT")\n    except BaseException:', '        pass\n    except BaseException:')], None),
    ("hr.py: a reused key is matched on the command only", "hr.py",
     'if (row["command"], row["payload"]) != (command, body):', 'if row["command"] != command:'),
    ("hr.py: termination keeps approved leave after the end date", "hr.py",
     "OR (status = 'approved' AND start_date > ?))", "OR (status = 'approved' AND start_date > ? AND 0))"),
    ("hr.py: termination keeps pending leave", "hr.py",
     "WHERE employee_id = ? AND (status = 'pending'", "WHERE employee_id = ? AND (0"),
    ("hr.py: accrual is not prorated", "hr.py",
     "employed = (year_end - max(start, year_start)).days + 1", "employed = (year_end - year_start).days + 1"),
    ("hr.py: requests count weekends", "hr.py",
     "if (start.weekday() + i) % 7 < 5)", "if True)"),
    ("hr.py: approved leave can be cancelled after it starts", "hr.py",
     'if req["status"] == "approved" and req["start_date"] <= today:',
     'if req["status"] == "approved" and req["start_date"] <= today and 0:'),
    ("hr.py: cancel ignores the today it is given", "hr.py",
     "today = datetime.date.today().isoformat() if today is None else today",
     "today = datetime.date.today().isoformat()"),
    ("hr.py: termination charges spanning leave in full", "hr.py",
     "kept = 2 * working_days(req[\"start_date\"], end_date)", "kept = req[\"half_days\"]"),
    ("hr.py: termination credits spanning leave in full", "hr.py",
     "            if kept:\n                replacement = conn", "            if 0:\n                replacement = conn"),
    ("hr.py: leave from an earlier employment can be cancelled", "hr.py",
     "if req[\"start_date\"] < _employment_start(conn, emp):", "if 0:"),
    ("hr.py: a rehire's employment starts at the first hire", "hr.py",
     "    return row[0] or emp[\"start_date\"]", "    return emp[\"start_date\"]"),
    ("hr.py: a half day defaults to no half", "hr.py",
     '    if half_day and half is None:\n        half = "am"', '    if 0:\n        half = "am"'),
    ("hr.py: different halves of one day clash", "hr.py",
     "AND NOT (half IS NOT NULL AND ? IS NOT NULL AND half <> ?)", "AND NOT (0 AND ? AND ?)"),
    ("hr.py: text is not normalised", "hr.py",
     '    return " ".join(unicodedata.normalize("NFC", value).split())', "    return value"),
    ("hr.py: emails keep their case", "hr.py",
     'return value.lower() if name == "work_email" else value', "return value"),
    ("hr.py: text has no length limit", "hr.py",
     "    if len(value) > limit:\n        raise ValueError(f\"{name} must be at most", "    if 0:\n        raise ValueError(f\"{name} must be at most"),
    ("hr.py: events may be dated before the last one", "hr.py",
     "    if value < last:\n", "    if 0:\n"),
    ("hr.py: the schema isn't checked at startup", "hr.py",
     "    verify_schema(conn)\n", "\n"),
]


def statements(sql: str) -> list[str]:
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf)
            buf = ""
    return out


def mutations() -> list[tuple[str, str, str, str]]:
    """(label, file, old, new): drop each trigger and unique index, and disable each CHECK, where a current
    database gets it from (its last definition)."""
    files = sorted((SRC / "migrations").glob("*.sql"))
    found, rules, renames = [], {}, {}
    for n, path in enumerate(files):
        for stmt in statements(path.read_text()):
            m = re.search(r"ALTER TABLE (\w+) RENAME TO (\w+)", stmt)
            if m:
                renames[m.group(1)] = m.group(2)
    dropped_later = {}                          # table -> index of the last file that drops it
    for n, path in enumerate(files):
        for stmt in statements(path.read_text()):
            m = re.search(r"^\s*DROP TABLE (?:IF EXISTS )?(\w+)", stmt, re.M)
            if m:
                dropped_later[m.group(1)] = n
    for n, path in enumerate(files):
        sql = path.read_text()
        for stmt in statements(sql):
            m = re.search(r"CREATE\s+(TRIGGER|UNIQUE INDEX)\s+(\w+)", stmt)
            if m:
                # A later definition under the same name replaces this one.
                rules[f"{m.group(1).lower()} {m.group(2)}"] = (f"migrations/{path.name}", stmt, "\n")
            table = re.search(r"CREATE TABLE (\w+)", stmt) or re.search(r"ALTER TABLE (\w+) ADD COLUMN", stmt)
            if table and dropped_later.get(table.group(1), -1) <= n:
                name = renames.get(table.group(1), table.group(1))
                for i, check in enumerate(re.finditer(r"CHECK\s*\(", stmt)):
                    depth, j = 1, check.end()
                    while depth:
                        depth += {"(": 1, ")": -1}.get(stmt[j], 0)
                        j += 1
                    body = stmt[check.start():j]
                    if stmt.count(body) == 1 and sql.count(body) == 1:
                        short = " ".join(body[6:-1].split())[:60]
                        found.append((f"check {name}: {short}", f"migrations/{path.name}", body, "CHECK (1)"))
    return [(label, *rule) for label, rule in rules.items()] + found + COMMAND_MUTATIONS


def run(label, file, old, new):
    with tempfile.TemporaryDirectory(prefix="hr_mutant_") as tmp:
        copy = Path(tmp) / "hr"
        shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
        if file:
            target = copy / file
            text = target.read_text()
            for a, b in (old if new is None else [(old, new)]):     # one replacement, or a list of them
                assert text.count(a) == 1, (label, a, text.count(a))
                text = text.replace(a, b)
            target.write_text(text)
        done = subprocess.run([sys.executable, "-B", "-c", HARNESS, str(copy)], capture_output=True, text=True,
                              timeout=600, cwd=tmp)
        fails = [line for line in done.stdout.splitlines() if line.startswith("FAIL")]
        if done.returncode != 0:
            fails.append(f"FAIL harness | {done.stderr.strip().splitlines()[-1] if done.stderr.strip() else ''}")
        return label, fails


def main():
    _, base = run("unchanged", None, None, None)
    if base:
        print("the unchanged copy fails:\n" + "\n".join(base))
        return 1
    muts = mutations()
    with ThreadPoolExecutor(JOBS) as pool:
        results = list(pool.map(lambda m: run(*m), muts))
    caught, redundant, survived = [], [], []
    for label, fails in results:
        if not fails:
            survived.append(label)
        elif all(WRONG_REASON in f for f in fails):
            redundant.append((label, fails))
        else:
            caught.append((label, fails))
    print(f"{len(muts)} removals: {len(caught)} caught, {len(redundant)} redundant, {len(survived)} survived\n")
    for label, fails in caught:
        print(f"caught     {label}  ({len(fails)} failing, e.g. {fails[0].split('|')[0][5:].strip()})")
    for label, fails in redundant:
        print(f"redundant  {label}\n           {fails[0]}")
    for label in survived:
        print(f"{'shadowed' if label in EXPECTED_SHADOWED else 'SURVIVED':<10} {label}")
    bad = False
    for name, got, expected in (("redundant", {label for label, _ in redundant}, EXPECTED_REDUNDANT),
                                ("survived", set(survived), EXPECTED_SHADOWED)):
        if got != expected:
            bad = True
            print(f"\n{name} set differs: unexpected {sorted(got - expected)}, missing {sorted(expected - got)}")
    print("\nOK" if not bad else "\nFAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
