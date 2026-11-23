"""
A walkthrough of the accounting module for a small cafe: open the books, invoice
a customer, enter a bill from the roastery, take a part payment, void a mistake,
and read the five reports back out of the journal.

    python -B modules/accounting/demo_cafe.py               # a throwaway database, deleted afterwards
    python -B modules/accounting/demo_cafe.py --db cafe.db  # keep the database to look at

Every command carries an idempotency key, so running it twice against the same
--db replays the same answers instead of invoicing anyone twice.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a  # noqa: E402

# The cafe's year, so the demo reads the same whatever today is.
TODAY = "2026-04-30"

CHART = [
    ("1000", "Bank current account", "asset", None),
    ("1010", "Till float", "asset", None),
    ("1100", "Accounts receivable", "asset", "accounts_receivable"),
    ("2100", "Accounts payable", "liability", "accounts_payable"),
    ("3000", "Owner's capital", "equity", None),
    ("4000", "Catering sales", "income", None),
    ("4010", "Counter sales", "income", None),
    ("5000", "Coffee and milk", "expense", None),
    ("5100", "Rent", "expense", None),
]


def money(cents: int) -> str:
    """Whole cents as the figure a person reads. Integer arithmetic only: nothing rounds."""
    sign = "-" if cents < 0 else ""
    whole = abs(cents)
    return f"{sign}{whole // 100}.{whole % 100:02d}"


def walkthrough(conn, say=print) -> dict:
    say(f"Accounting schema version {a.init_db(conn)['to_version']}\n")

    say("1. The chart of accounts")
    for code, name, kind, role in CHART:
        a.add_account(conn, f"demo:account:{code}", code, name, kind, role=role)
    for account in a.accounts(conn):
        mark = f"  [{account['role']}]" if account["role"] else ""
        say(f"   {account['code']} {account['name']:<22} {account['account_type']}{mark}")

    say("\n2. The owner puts 5,000.00 in, as one balanced entry")
    opening = a.post_journal_entry(conn, "demo:opening", "2026-01-01", "Owner's opening capital", [
        {"account_code": "1000", "debit_cents": 500_000},
        {"account_code": "3000", "credit_cents": 500_000},
    ])
    for line in opening["lines"]:
        side = f"debit {money(line['debit_cents'])}" if line["debit_cents"] else f"credit {money(line['credit_cents'])}"
        say(f"   {line['account_code']} {line['account_name']:<22} {side}")

    say("\n3. A catering customer, on 14-day terms")
    customer = a.add_customer(conn, "demo:customer:office", "Northgate Offices", payment_terms_days=14,
                              email="accounts@northgate.example",
                              terms="14 days from invoice. Cancellations 48 hours ahead.")
    say(f"   #{customer['id']} {customer['name']}, {customer['payment_terms_days']}-day terms")
    say(f"   terms on file: {customer['terms']}")

    say("\n4. Invoice INV-2026-014: three breakfast runs and a cake")
    invoice = a.create_invoice(conn, "demo:invoice:14", customer["id"], "INV-2026-014", "2026-03-02", [
        {"description": "Breakfast catering, 20 people", "quantity": 3, "unit_price_cents": 18_000,
         "account_code": "4000"},
        {"description": "Birthday cake", "quantity": 1, "unit_price_cents": 4_500, "account_code": "4000"},
    ])
    for line in invoice["lines"]:
        say(f"   {line['quantity']} x {money(line['unit_price_cents'])}  {line['description']}"
            f"  = {money(line['amount_cents'])}")
    say(f"   total {money(invoice['total_cents'])}, {invoice['status']}, due {invoice['due_date']}")
    invoice = a.send_invoice(conn, invoice["invoice_id"], "demo:invoice:14:send")
    say(f"   sent: entry #{invoice['entry_id']} debits receivables and credits catering sales")

    say("\n5. Northgate pays 400.00 of the 585.00")
    invoice = a.record_invoice_payment(conn, invoice["invoice_id"], 40_000, "2026-03-20",
                                       a.account_by_code(conn, "1000")["account_id"],
                                       "demo:invoice:14:pay1", reference="BACS 9981")
    say(f"   {invoice['status']}, paid {money(invoice['amount_paid_cents'])}, "
        f"outstanding {money(invoice['outstanding_cents'])}")
    say(f"   on {TODAY} it reads as: {a.invoice(conn, invoice['invoice_id'], as_of=TODAY)['effective_status']}")

    say("\n6. The roastery's bill, and the rent")
    roastery = a.add_supplier(conn, "demo:supplier:roastery", "Dockside Roastery", payment_terms_days=30)
    landlord = a.add_supplier(conn, "demo:supplier:landlord", "Marsh Lane Properties", payment_terms_days=7)
    beans = a.create_bill(conn, "demo:bill:beans", roastery["id"], "DR-4471", "2026-03-05", [
        {"description": "Beans, 12 kg", "quantity": 12, "unit_price_cents": 2_250, "account_code": "5000"},
    ])
    beans = a.receive_bill(conn, beans["bill_id"], "demo:bill:beans:receive")
    rent = a.create_bill(conn, "demo:bill:rent", landlord["id"], "MLP-03", "2026-03-01", [
        {"description": "March rent", "unit_price_cents": 95_000, "account_code": "5100"},
    ])
    rent = a.receive_bill(conn, rent["bill_id"], "demo:bill:rent:receive")
    rent = a.record_bill_payment(conn, rent["bill_id"], 95_000, "2026-03-06",
                                 a.account_by_code(conn, "1000")["account_id"], "demo:bill:rent:pay")
    say(f"   {beans['bill_number']} from {beans['supplier']}: {money(beans['total_cents'])}, "
        f"{beans['status']}, due {beans['due_date']}")
    say(f"   {rent['bill_number']} from {rent['supplier']}: {money(rent['total_cents'])}, {rent['status']}")

    say("\n7. A bill entered twice is voided, and the void reverses its entry")
    duplicate = a.create_bill(conn, "demo:bill:dup", roastery["id"], "DR-4471-DUP", "2026-03-05", [
        {"description": "Beans, 12 kg (entered twice by mistake)", "quantity": 12, "unit_price_cents": 2_250,
         "account_code": "5000"},
    ])
    duplicate = a.receive_bill(conn, duplicate["bill_id"], "demo:bill:dup:receive")
    payable_before = a.account_by_code(conn, "2100")["balance_cents"]
    duplicate = a.void_bill(conn, duplicate["bill_id"], "entered twice", "demo:bill:dup:void")
    payable_after = a.account_by_code(conn, "2100")["balance_cents"]
    say(f"   {duplicate['status']}: entry #{duplicate['entry_id']} reversed by #{duplicate['void_entry_id']}")
    say(f"   payables went {money(payable_before)} -> {money(payable_after)}, and both entries are still on file")

    say("\n8. Trial balance")
    trial = a.trial_balance(conn, as_of=TODAY)
    for row in trial["rows"]:
        say(f"   {row['code']} {row['name']:<22} debit {money(row['debit_cents']):>9}"
            f"   credit {money(row['credit_cents']):>9}")
    say(f"   {'':26} debit {money(trial['total_debit_cents']):>9}"
        f"   credit {money(trial['total_credit_cents']):>9}   balanced: {trial['balanced']}")

    say("\n9. Profit and loss, 1 January to 31 March")
    pl = a.profit_and_loss(conn, "2026-01-01", "2026-03-31")
    for row in pl["income"] + pl["expenses"]:
        say(f"   {row['code']} {row['name']:<22} {money(row['balance_cents']):>10}")
    say(f"   income {money(pl['total_income_cents'])} - expenses {money(pl['total_expense_cents'])}"
        f" = {money(pl['net_income_cents'])}")

    say("\n10. Balance sheet")
    sheet = a.balance_sheet(conn, as_of=TODAY)
    for label, rows in (("assets", sheet["assets"]), ("liabilities", sheet["liabilities"]),
                        ("equity", sheet["equity"])):
        for row in rows:
            say(f"   {label:<12} {row['code']} {row['name']:<22} {money(row['balance_cents']):>10}")
    say(f"   assets {money(sheet['total_assets_cents'])} = liabilities {money(sheet['total_liabilities_cents'])}"
        f" + equity {money(sheet['total_equity_cents'])}"
        f" + profit so far {money(sheet['retained_earnings_cents'])}  -> {sheet['balanced']}")

    say(f"\n11. Who owes what on {TODAY}")
    receivable = a.ar_aging(conn, as_of=TODAY)
    for row in receivable["by_customer"]:
        say(f"   {row['customer']:<22} not due {money(row['current']):>9}   1-30 {money(row['1_30']):>9}"
            f"   31-60 {money(row['31_60']):>9}   total {money(row['total_cents']):>9}")
    payable = a.ap_aging(conn, as_of=TODAY)
    for row in payable["by_supplier"]:
        say(f"   {row['supplier']:<22} not due {money(row['current']):>9}   1-30 {money(row['1_30']):>9}"
            f"   31-60 {money(row['31_60']):>9}   total {money(row['total_cents']):>9}")
    say(f"   owed to us {money(receivable['totals']['total_cents'])}, "
        f"and the receivables account says "
        f"{money(a.account_balance(conn, a.account_by_code(conn, '1100')['account_id'], as_of=TODAY)['balance_cents'])}")

    say("\n12. Payroll would post here (modules/accounting/README.md, \"Hook for HR payroll\"):"
        " accounting.post_payroll_entry(). Nothing calls it yet.")
    return {"invoice": invoice, "beans": beans, "rent": rent, "voided": duplicate, "trial_balance": trial,
            "profit_and_loss": pl, "balance_sheet": sheet, "ar_aging": receivable, "ap_aging": payable}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", help="SQLite file to use (kept afterwards); default is a throwaway temp file")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="accounting_demo_", ignore_cleanup_errors=True) as tmp:
        path = args.db or os.path.join(tmp, "cafe.db")
        conn = a.connect(path)
        try:
            walkthrough(conn)
        finally:
            conn.close()
        print(f"\nDatabase: {path}{'' if args.db else ' (deleted on exit)'}")


if __name__ == "__main__":
    main()
