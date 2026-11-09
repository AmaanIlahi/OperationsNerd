"""
Fund accounting walkthrough for an environmental non-profit: unrestricted and
forest-project restricted grants, spending from the restricted fund, and a release
to unrestricted when the project ends.

    python -B modules/accounting/demo_nonprofit.py
    python -B modules/accounting/demo_nonprofit.py --db nonprofit.db
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a  # noqa: E402

TODAY = "2026-06-30"

CHART = [
    ("1000", "Operating bank account", "asset", None),
    ("1100", "Accounts receivable", "asset", "accounts_receivable"),
    ("2100", "Accounts payable", "liability", "accounts_payable"),
    ("3000", "Unrestricted net assets", "equity", None),
    ("3100", "Donor-restricted net assets", "equity", None),
    ("3200", "Interfund clearing", "liability", None),
    ("4100", "Unrestricted grants", "income", None),
    ("4110", "Forest project grants", "income", None),
    ("5000", "Forest management", "expense", None),
    ("5100", "General administration", "expense", None),
]


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole = abs(cents)
    return f"{sign}{whole // 100}.{whole % 100:02d}"


def walkthrough(conn, say=print) -> dict:
    say(f"Accounting schema version {a.init_db(conn)['to_version']}\n")
    default_fund = a.fund(conn, a.default_fund_id(conn))

    say("1. Chart of accounts")
    for code, name, kind, role in CHART:
        a.add_account(conn, f"demo:acct:{code}", code, name, kind, role=role)

    say("\n2. Funds (General already exists from migration)")
    say(f"   Default: {default_fund['code']} {default_fund['name']} ({default_fund['restriction_type']})")
    forest = a.add_fund(conn, "demo:fund:forest", "FOREST", "Forest restoration project",
                        "temporarily_restricted", purpose_description="Management of West Ridge forest",
                        grantor="Green Earth Foundation", start_date="2026-01-01", end_date="2028-12-31")
    say(f"   Added: {forest['code']} {forest['name']}, grantor {forest['grantor']}")

    say("\n3. Unrestricted operating grant 25,000.00")
    a.record_grant_receipt(conn, "demo:grant:unres", default_fund["fund_id"], "2026-01-15", 2_500_000,
                           a.account_by_code(conn, "1000")["account_id"],
                           a.account_by_code(conn, "4100")["account_id"],
                           "Annual unrestricted support", grantor="City council")

    say("\n4. Restricted forest grant 80,000.00")
    a.record_grant_receipt(conn, "demo:grant:forest", forest["fund_id"], "2026-02-01", 8_000_000,
                           a.account_by_code(conn, "1000")["account_id"],
                           a.account_by_code(conn, "4110")["account_id"],
                           "West Ridge forest management grant", grantor="Green Earth Foundation")

    say("\n5. Forest contractor bill 12,000.00 (restricted fund)")
    supplier = a.add_supplier(conn, "demo:supplier", "TrailWorks Ltd")
    bill = a.create_bill(conn, "demo:bill", supplier["id"], "TW-884", "2026-03-10", [
        {"description": "Trail maintenance", "unit_price_cents": 1_200_000, "account_code": "5000"},
    ], fund_id=forest["fund_id"])
    a.receive_bill(conn, bill["bill_id"], "demo:bill:recv")

    say("\n6. Small admin expense from unrestricted fund (manual entry)")
    a.post_journal_entry(conn, "demo:admin", "2026-04-01", "Office supplies", [
        {"account_code": "5100", "debit_cents": 45_000, "fund_id": default_fund["fund_id"]},
        {"account_code": "1000", "credit_cents": 45_000, "fund_id": default_fund["fund_id"]},
    ])

    say("\n7. Release 50,000.00 from forest restriction (purpose milestones met)")
    a.release_from_restriction(conn, "demo:release", forest["fund_id"], 5_000_000, "2026-06-01",
                               "Release per grant completion report",
                               a.account_by_code(conn, "3100")["account_id"],
                               a.account_by_code(conn, "3000")["account_id"],
                               a.account_by_code(conn, "3200")["account_id"])

    say("\n8. Fund balances")
    for row in a.fund_balances(conn, as_of=TODAY)["funds"]:
        say(f"   {row['code']:<8} net assets {money(row['net_assets_cents']):>12}"
            f"  available {money(row['available_cents']):>12}")

    say("\n9. Statement of activities (YTD)")
    soa = a.statement_of_activities(conn, "2026-01-01", TODAY)
    say(f"   Without restrictions: net {money(soa['net_without_cents'])}")
    say(f"   With restrictions:    net {money(soa['net_with_cents'])}")

    tb = a.trial_balance(conn, as_of=TODAY)
    say(f"\n10. Trial balance balanced: {tb['balanced']}, totals {money(tb['total_debit_cents'])}")
    return {"forest_fund_id": forest["fund_id"], "default_fund_id": default_fund["fund_id"]}


def main():
    parser = argparse.ArgumentParser(description="Non-profit fund accounting demo")
    parser.add_argument("--db", help="SQLite database path (otherwise throwaway)")
    args = parser.parse_args()
    if args.db:
        conn = a.connect(args.db)
        walkthrough(conn)
        conn.close()
        print(f"\nDatabase saved at {args.db}")
        return
    path = os.path.join(tempfile.mkdtemp(prefix="accounting_nonprofit_"), "nonprofit.db")
    conn = a.connect(path)
    walkthrough(conn)
    conn.close()
    print(f"\nThrowaway database at {path}")


if __name__ == "__main__":
    main()
