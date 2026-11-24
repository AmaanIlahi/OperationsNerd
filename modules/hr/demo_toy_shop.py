"""
A walkthrough of the HR module for a small toy shop: hire three people, set up
leave types, accrue this year's paid time off, approve one request and watch the
module refuse one that the employee doesn't have the balance for.

    python -B modules/hr/demo_toy_shop.py              # a throwaway database, deleted afterwards
    python -B modules/hr/demo_toy_shop.py --db shop.db # keep the database to look at

Every command carries an idempotency key, so running it twice against the
same --db replays the same answers instead of hiring everyone again.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hr as h  # noqa: E402

YEAR = 2026


def n(days) -> str:
    """20.0 -> '20', 5.5 -> '5.5'."""
    return f"{days:g}"


def walkthrough(conn, say=print) -> dict:
    say(f"HR schema version {h.init_db(conn)['to_version']}\n")

    say("1. Hiring the team at Tiny Tots Toys")
    owner = h.add_employee(conn, "demo:hire:owner", employee_number="TT-001", first_name="Priya", last_name="Shah",
                           job_title="Owner", department="Management", start_date="2023-04-03",
                           work_email="priya@tinytots.example")
    sam = h.add_employee(conn, "demo:hire:sam", employee_number="TT-002", first_name="Sam", last_name="Okafor",
                         job_title="Shop assistant", department="Shop floor", manager_id=owner["employee_id"],
                         employment_type="part_time", start_date="2025-02-03")
    lee = h.add_employee(conn, "demo:hire:lee", employee_number="TT-003", first_name="Lee", last_name="Chen",
                         job_title="Weekend helper", department="Shop floor", manager_id=owner["employee_id"],
                         employment_type="temporary", start_date="2026-09-07")
    for e in (owner, sam, lee):
        boss = f", reports to {e['manager_name']}" if e["manager_name"] else ""
        say(f"   #{e['employee_id']} {e['employee_number']} {e['first_name']} {e['last_name']}: {e['job_title']} "
            f"({e['employment_type']}, from {e['start_date']}{boss})")

    say("\n2. Leave types")
    # "pto" and its yearly allowance are the HR pack's pto_annual_days setting (PR #21).
    h.create_leave_type(conn, "pto", "Paid time off", "demo:type:pto", annual_allowance_days=20)
    h.create_leave_type(conn, "unpaid", "Unpaid leave", "demo:type:unpaid", paid=False, tracks_balance=False)
    for t in h.leave_types(conn):
        allowance = f"{n(t['annual_allowance_days'])} days a year" if t["tracks_balance"] else "no balance"
        say(f"   {t['code']}: {t['name']}, {'paid' if t['paid'] else 'unpaid'}, {allowance}")

    say(f"\n3. Accruing {YEAR} paid time off (prorated from the start date in the first year)")
    for e in (owner, sam, lee):
        a = h.accrue_annual_leave(conn, e["employee_id"], "pto", YEAR, f"demo:accrue:{e['employee_id']}:{YEAR}")
        say(f"   {e['first_name']}: {n(a['days'])} days{' (prorated)' if a['prorated'] else ''}")

    say("\n4. Sam asks for Monday 2 to Wednesday 4 November off; Priya approves")
    req = h.request_leave(conn, sam["employee_id"], "pto", "2026-11-02", "2026-11-04", "demo:request:sam",
                          note="visiting family", as_of="2026-10-20")
    say(f"   request #{req['request_id']}: {n(req['days'])} working days, {req['status']}")
    ok = h.approve_leave(conn, req["request_id"], "demo:approve:sam", approver="Priya", as_of="2026-10-21")
    say(f"   request #{ok['request_id']}: {ok['status']} by Priya")

    say("\n5. Lee asks for two weeks in December, more than the 6 days accrued so far")
    refused = None
    try:
        h.request_leave(conn, lee["employee_id"], "pto", "2026-12-14", "2026-12-25", "demo:request:lee", as_of="2026-10-22")
    except h.InsufficientBalance as e:
        refused = str(e)
        say(f"   refused: {refused}")
    say(f"   Lee's requests on file: {h.leave_requests(conn, employee_id=lee['employee_id'])} (nothing was written)")

    say("\n6. Balances")
    balances = {}
    for e in (owner, sam, lee):
        b = h.leave_balance(conn, e["employee_id"], "pto")
        balances[e["first_name"]] = b
        say(f"   {e['first_name']}: accrued {n(b['accrued'])}, taken {n(b['taken'])}, "
            f"pending {n(b['pending'])}, available {n(b['available'])}")

    say("\n7. Sam's leave ledger (append-only)")
    ledger = h.leave_ledger(conn, sam["employee_id"])
    for entry in ledger:
        req_part = f" request #{entry['request_id']}" if entry["request_id"] else ""
        say(f"   {entry['entry_type']:<8} {n(entry['days']):>4} days{req_part}  {entry['reason'] or ''}".rstrip())

    say("\n8. Sam's employment history")
    h.update_employee(conn, sam["employee_id"], {"job_title": "Senior shop assistant", "employment_type": "full_time"},
                      "2026-11-09", "demo:promote:sam", reason="promotion")
    history = h.employment_history(conn, sam["employee_id"])
    for ev in history:
        say(f"   v{ev['version']} {ev['event']:<8} {ev['effective_date']}  {ev['changes'] if ev['version'] > 1 else ''}"
            .rstrip())
    return {"approved": ok, "refused": refused, "balances": balances, "ledger": ledger, "history": history}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="SQLite file to use (kept afterwards); default is a throwaway temp file")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="hr_demo_", ignore_cleanup_errors=True) as tmp:
        path = args.db or os.path.join(tmp, "toy_shop.db")
        conn = h.connect(path)
        try:
            walkthrough(conn)
        finally:
            conn.close()
        print(f"\nDatabase: {path}{'' if args.db else ' (deleted on exit)'}")


if __name__ == "__main__":
    main()
