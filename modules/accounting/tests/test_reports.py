"""
Accounting module: the five reports. Each one is checked against figures worked
out by hand in the test, and against the ledger it is read from: the trial
balance always balances, the balance sheet always adds up, and the aging
reports always agree with the receivable and payable accounts. Run from the
repo root with the venv (pytest also collects these):

    python -B modules/accounting/tests/test_reports.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a
from test_ledger import chart, rejected

TMP = tempfile.TemporaryDirectory(prefix="accounting_reports_", ignore_cleanup_errors=True)


def books(name: str) -> sqlite3.Connection:
    conn = a.connect(os.path.join(TMP.name, name + ".db"))
    a.init_db(conn)
    chart(conn)
    return conn


def bank(conn) -> int:
    return a.account_by_code(conn, "1000")["account_id"]


def a_year(conn) -> dict:
    """A small year of trading, with every figure written down here so the reports can be checked by hand.

    capital           5,000.00 in            (debit bank,        credit capital)
    invoice 1           300.00 sold 03-01    (debit receivable,  credit sales)     paid 03-20 in full
    invoice 2           120.00 sold 04-01    (debit receivable,  credit sales)     unpaid, due 04-15
    bill 1              270.00 bought 03-05  (debit rent,        credit payable)   paid 03-10 in full
    bill 2              500.00 bought 05-01  (debit rent,        credit payable)   50.00 paid 05-20
    """
    customer = a.add_customer(conn, "c1", "Northgate Offices")
    supplier = a.add_supplier(conn, "s1", "Dockside Roastery")
    a.post_journal_entry(conn, "capital", "2026-01-01", "Opening capital", [
        {"account_code": "1000", "debit_cents": 500_000}, {"account_code": "3000", "credit_cents": 500_000}])

    def invoice(number, date, due, cents, key):
        draft = a.create_invoice(conn, f"inv:{key}", customer["id"], number, date, [
            {"description": "Catering", "unit_price_cents": cents, "account_code": "4000"}], due_date=due)
        return a.send_invoice(conn, draft["invoice_id"], f"send:{key}")

    def bill(number, date, due, cents, key):
        draft = a.create_bill(conn, f"bill:{key}", supplier["id"], number, date, [
            {"description": "Rent", "unit_price_cents": cents, "account_code": "5000"}], due_date=due)
        return a.receive_bill(conn, draft["bill_id"], f"receive:{key}")

    one = invoice("INV-001", "2026-03-01", "2026-03-15", 30_000, "1")
    two = invoice("INV-002", "2026-04-01", "2026-04-15", 12_000, "2")
    a.record_invoice_payment(conn, one["invoice_id"], 30_000, "2026-03-20", bank(conn), "pay:inv1")
    first = bill("DR-1", "2026-03-05", "2026-03-20", 27_000, "1")
    second = bill("DR-2", "2026-05-01", "2026-05-31", 50_000, "2")
    a.record_bill_payment(conn, first["bill_id"], 27_000, "2026-03-10", bank(conn), "pay:bill1")
    a.record_bill_payment(conn, second["bill_id"], 5_000, "2026-05-20", bank(conn), "pay:bill2")
    return {"customer": customer, "supplier": supplier, "invoices": [one, two], "bills": [first, second]}


# ---------------------------------------------------------------- trial balance

def test_the_trial_balance_lists_both_totals_and_they_are_equal():
    conn = books("trial")
    a_year(conn)
    report = a.trial_balance(conn, as_of="2026-12-31")
    rows = {row["code"]: row for row in report["rows"]}
    # Bank: 5,000 in, 300 received, less 270 and 50 paid out.
    assert rows["1000"]["debit_cents"] == 500_000 + 30_000
    assert rows["1000"]["credit_cents"] == 27_000 + 5_000
    assert rows["1000"]["balance_cents"] == 498_000
    # Receivables: 420 invoiced, 300 received, so 120 still owed.
    assert (rows["1100"]["debit_cents"], rows["1100"]["credit_cents"]) == (42_000, 30_000)
    assert rows["1100"]["balance_cents"] == 12_000
    # Payables: 770 billed, 320 paid, so 450 still owed. A liability's natural side is the credit.
    assert (rows["2100"]["debit_cents"], rows["2100"]["credit_cents"]) == (32_000, 77_000)
    assert rows["2100"]["balance_cents"] == 45_000
    assert rows["4000"]["balance_cents"] == 42_000 and rows["5000"]["balance_cents"] == 77_000
    assert report["total_debit_cents"] == report["total_credit_cents"] and report["balanced"]
    assert report["total_debit_cents"] == 500_000 + 30_000 + 42_000 + 32_000 + 77_000


def test_the_trial_balance_leaves_out_untouched_accounts_unless_asked():
    conn = books("trial_empty")
    a.post_journal_entry(conn, "e1", "2026-01-01", "Capital", [
        {"account_code": "1000", "debit_cents": 100}, {"account_code": "3000", "credit_cents": 100}])
    assert [r["code"] for r in a.trial_balance(conn, as_of="2026-01-01")["rows"]] == ["1000", "3000"]
    everything = a.trial_balance(conn, as_of="2026-01-01", include_empty=True)
    assert [r["code"] for r in everything["rows"]] == ["1000", "1010", "1100", "2100", "3000", "4000", "5000"]
    assert everything["total_debit_cents"] == everything["total_credit_cents"] == 100
    untouched = [r for r in everything["rows"] if r["code"] == "1010"][0]
    assert untouched["debit_cents"] == untouched["credit_cents"] == untouched["balance_cents"] == 0


def test_the_trial_balance_is_cut_off_at_its_date():
    conn = books("trial_as_of")
    a_year(conn)
    assert a.trial_balance(conn, as_of="2026-02-28")["total_debit_cents"] == 500_000
    march = a.trial_balance(conn, as_of="2026-03-31")
    assert {r["code"] for r in march["rows"]} == {"1000", "1100", "2100", "3000", "4000", "5000"}
    assert march["balanced"]
    # Before anything was posted the report is empty and still balances.
    empty = a.trial_balance(conn, as_of="2025-12-31")
    assert empty["rows"] == [] and empty["total_debit_cents"] == 0 and empty["balanced"]


def test_every_report_date_has_to_be_a_real_day():
    conn = books("report_dates")
    for bad in ("2026-02-30", "2026-13-01", "01/02/2026", ""):
        rejected(lambda v=bad: a.trial_balance(conn, as_of=v), "must be a date as 'YYYY-MM-DD'", ValueError)
        rejected(lambda v=bad: a.balance_sheet(conn, as_of=v), "must be a date as 'YYYY-MM-DD'", ValueError)
        rejected(lambda v=bad: a.ar_aging(conn, as_of=v), "must be a date as 'YYYY-MM-DD'", ValueError)
        rejected(lambda v=bad: a.ap_aging(conn, as_of=v), "must be a date as 'YYYY-MM-DD'", ValueError)
        rejected(lambda v=bad: a.profit_and_loss(conn, v, "2026-12-31"), "must be a date as", ValueError)
    rejected(lambda: a.profit_and_loss(conn, "2026-12-31", "2026-01-01"),
             "to_date 2026-01-01 is before from_date 2026-12-31", ValueError)


# ---------------------------------------------------------------- profit and loss

def test_profit_and_loss_covers_the_range_inclusively():
    conn = books("pl")
    a_year(conn)
    year = a.profit_and_loss(conn, "2026-01-01", "2026-12-31")
    assert year["total_income_cents"] == 42_000                    # both invoices
    assert year["total_expense_cents"] == 77_000                   # both bills
    assert year["net_income_cents"] == -35_000
    assert [row["code"] for row in year["income"]] == ["4000"]
    assert [row["code"] for row in year["expenses"]] == ["5000"]
    # Only what was posted inside the range: March alone is invoice 1 and bill 1.
    march = a.profit_and_loss(conn, "2026-03-01", "2026-03-31")
    assert march["total_income_cents"] == 30_000 and march["total_expense_cents"] == 27_000
    assert march["net_income_cents"] == 3_000
    # The boundaries are included: a range of the single day invoice 2 was sent sees exactly it.
    one_day = a.profit_and_loss(conn, "2026-04-01", "2026-04-01")
    assert one_day["total_income_cents"] == 12_000 and one_day["total_expense_cents"] == 0
    quiet = a.profit_and_loss(conn, "2026-06-01", "2026-06-30")
    assert quiet["income"] == [] and quiet["expenses"] == [] and quiet["net_income_cents"] == 0


def test_profit_and_loss_follows_the_invoice_not_the_payment():
    """Income is recognised when the invoice is sent, not when the money turns up, which is the point of
    having receivables at all."""
    conn = books("accrual")
    customer = a.add_customer(conn, "c", "Northgate Offices")
    draft = a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 30_000, "account_code": "4000"}], due_date="2026-03-31")
    a.send_invoice(conn, draft["invoice_id"], "send")
    a.record_invoice_payment(conn, draft["invoice_id"], 30_000, "2026-07-01", bank(conn), "pay")
    assert a.profit_and_loss(conn, "2026-03-01", "2026-03-31")["total_income_cents"] == 30_000
    assert a.profit_and_loss(conn, "2026-07-01", "2026-07-31")["total_income_cents"] == 0
    assert a.account_balance(conn, bank(conn), as_of="2026-06-30")["balance_cents"] == 0
    assert a.account_balance(conn, bank(conn), as_of="2026-07-01")["balance_cents"] == 30_000


def test_a_credit_against_an_income_account_reduces_income():
    conn = books("refund")
    a_year(conn)
    before = a.profit_and_loss(conn, "2026-01-01", "2026-12-31")["total_income_cents"]
    a.post_journal_entry(conn, "refund", "2026-06-01", "Goodwill refund to Northgate", [
        {"account_code": "4000", "debit_cents": 5_000}, {"account_code": "1000", "credit_cents": 5_000}])
    after = a.profit_and_loss(conn, "2026-01-01", "2026-12-31")
    assert after["total_income_cents"] == before - 5_000
    assert after["income"][0]["debit_cents"] == 5_000
    assert a.trial_balance(conn, as_of="2026-12-31")["balanced"]


# ---------------------------------------------------------------- balance sheet

def test_the_balance_sheet_adds_up_with_the_profit_shown_separately():
    conn = books("sheet")
    a_year(conn)
    sheet = a.balance_sheet(conn, as_of="2026-12-31")
    assert sheet["total_assets_cents"] == 498_000 + 12_000          # bank plus what is still owed to us
    assert sheet["total_liabilities_cents"] == 45_000               # what is still owed to suppliers
    assert sheet["total_equity_cents"] == 500_000
    assert sheet["retained_earnings_cents"] == -35_000              # the year's loss, not closed to equity
    assert sheet["total_assets_cents"] == (sheet["total_liabilities_cents"] + sheet["total_equity_cents"]
                                           + sheet["retained_earnings_cents"])
    assert sheet["balanced"]
    assert [row["code"] for row in sheet["assets"]] == ["1000", "1100"]
    assert [row["code"] for row in sheet["liabilities"]] == ["2100"]
    assert [row["code"] for row in sheet["equity"]] == ["3000"]


def test_the_balance_sheet_balances_at_every_date_of_the_year():
    """Every entry balances, so the sheet does too, whatever day it is cut at."""
    conn = books("sheet_every_day")
    a_year(conn)
    import datetime
    day = datetime.date(2025, 12, 31)
    while day <= datetime.date(2026, 12, 31):
        as_of = day.isoformat()
        sheet = a.balance_sheet(conn, as_of=as_of)
        assert sheet["balanced"], (as_of, sheet)
        assert a.trial_balance(conn, as_of=as_of)["balanced"], as_of
        day += datetime.timedelta(days=7)


def test_the_retained_profit_is_everything_up_to_the_date_not_just_the_year():
    conn = books("retained")
    a.post_journal_entry(conn, "y1", "2025-06-01", "Last year's sale", [
        {"account_code": "1000", "debit_cents": 10_000}, {"account_code": "4000", "credit_cents": 10_000}])
    a.post_journal_entry(conn, "y2", "2026-06-01", "This year's sale", [
        {"account_code": "1000", "debit_cents": 4_000}, {"account_code": "4000", "credit_cents": 4_000}])
    assert a.balance_sheet(conn, as_of="2025-12-31")["retained_earnings_cents"] == 10_000
    assert a.balance_sheet(conn, as_of="2026-12-31")["retained_earnings_cents"] == 14_000
    assert a.profit_and_loss(conn, "2026-01-01", "2026-12-31")["net_income_cents"] == 4_000


# ---------------------------------------------------------------- aging

def test_ar_aging_buckets_each_invoice_by_how_late_it_is():
    conn = books("ar")
    customer = a.add_customer(conn, "c", "Northgate Offices")
    due_dates = {"current": "2026-07-01", "1_30": "2026-06-10", "31_60": "2026-05-10",
                 "61_90": "2026-04-10", "over_90": "2026-01-10"}
    for n, (bucket, due) in enumerate(due_dates.items(), start=1):
        draft = a.create_invoice(conn, f"inv:{n}", customer["id"], f"INV-00{n}", "2026-01-05", [
            {"description": "Catering", "unit_price_cents": 1_000 * n, "account_code": "4000"}], due_date=due)
        a.send_invoice(conn, draft["invoice_id"], f"send:{n}")
    report = a.ar_aging(conn, as_of="2026-06-15")
    assert [(d["invoice_number"], d["bucket"]) for d in report["documents"]] == [
        ("INV-005", "over_90"), ("INV-004", "61_90"), ("INV-003", "31_60"), ("INV-002", "1_30"),
        ("INV-001", "current")]
    assert report["totals"] == {"current": 1_000, "1_30": 2_000, "31_60": 3_000, "61_90": 4_000,
                               "over_90": 5_000, "total_cents": 15_000}
    assert [row["customer"] for row in report["by_customer"]] == ["Northgate Offices"]
    assert report["by_customer"][0]["total_cents"] == 15_000
    # INV-001 falls due on 2026-07-01, so the day a bucket turns over: on the due date itself it is not
    # late, 30 days after is still 1_30, and the 31st day moves it on.
    def invoice_one(as_of):
        return [d for d in a.ar_aging(conn, as_of=as_of)["documents"] if d["invoice_number"] == "INV-001"][0]

    assert invoice_one("2026-07-01")["days_late"] == 0 and invoice_one("2026-07-01")["bucket"] == "current"
    assert invoice_one("2026-07-02")["bucket"] == "1_30"
    assert invoice_one("2026-07-31")["days_late"] == 30 and invoice_one("2026-07-31")["bucket"] == "1_30"
    assert invoice_one("2026-08-01")["days_late"] == 31 and invoice_one("2026-08-01")["bucket"] == "31_60"


def test_the_aging_totals_equal_the_receivable_and_payable_accounts():
    conn = books("aging_matches")
    a_year(conn)
    for as_of in ("2026-03-01", "2026-03-20", "2026-04-02", "2026-05-20", "2026-12-31"):
        receivable = a.account_balance(conn, a.account_by_code(conn, "1100")["account_id"], as_of=as_of)
        payable = a.account_balance(conn, a.account_by_code(conn, "2100")["account_id"], as_of=as_of)
        assert a.ar_aging(conn, as_of=as_of)["totals"]["total_cents"] == receivable["balance_cents"], as_of
        assert a.ap_aging(conn, as_of=as_of)["totals"]["total_cents"] == payable["balance_cents"], as_of


def test_aging_is_what_the_books_said_on_the_date_not_what_they_say_now():
    conn = books("aging_history")
    customer = a.add_customer(conn, "c", "Northgate Offices")
    draft = a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 30_000, "account_code": "4000"}], due_date="2026-03-31")
    a.send_invoice(conn, draft["invoice_id"], "send")
    a.record_invoice_payment(conn, draft["invoice_id"], 30_000, "2026-06-01", bank(conn), "pay")
    # Before it was sent: nothing owed. After it was sent, and before the payment: the whole amount.
    assert a.ar_aging(conn, as_of="2026-02-28")["totals"]["total_cents"] == 0
    assert a.ar_aging(conn, as_of="2026-03-01")["totals"]["total_cents"] == 30_000
    assert a.ar_aging(conn, as_of="2026-05-31")["totals"]["total_cents"] == 30_000
    assert a.ar_aging(conn, as_of="2026-06-01")["totals"]["total_cents"] == 0
    # And the invoice is 'paid' now, so a report run today shows nothing outstanding either.
    assert a.ar_aging(conn)["totals"]["total_cents"] == 0


def test_a_voided_document_drops_out_of_the_aging_from_the_day_it_was_voided():
    conn = books("aging_void")
    customer = a.add_customer(conn, "c", "Northgate Offices")
    draft = a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 30_000, "account_code": "4000"}], due_date="2026-03-31")
    a.send_invoice(conn, draft["invoice_id"], "send")
    a.void_invoice(conn, draft["invoice_id"], "wrong customer", "void", entry_date="2026-04-10")
    assert a.ar_aging(conn, as_of="2026-04-09")["totals"]["total_cents"] == 30_000
    assert a.ar_aging(conn, as_of="2026-04-10")["totals"]["total_cents"] == 0
    # The receivable account agrees on both days, because the void is a reversal in the journal.
    receivable = a.account_by_code(conn, "1100")["account_id"]
    assert a.account_balance(conn, receivable, as_of="2026-04-09")["balance_cents"] == 30_000
    assert a.account_balance(conn, receivable, as_of="2026-04-10")["balance_cents"] == 0


def test_drafts_are_never_in_the_aging_because_nothing_is_owed_yet():
    conn = books("aging_draft")
    customer = a.add_customer(conn, "c", "Northgate Offices")
    supplier = a.add_supplier(conn, "s", "Dockside Roastery")
    a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 30_000, "account_code": "4000"}], due_date="2026-03-02")
    a.create_bill(conn, "bill", supplier["id"], "DR-1", "2026-03-01", [
        {"description": "Rent", "unit_price_cents": 10_000, "account_code": "5000"}], due_date="2026-03-02")
    assert a.ar_aging(conn, as_of="2026-12-31")["documents"] == []
    assert a.ap_aging(conn, as_of="2026-12-31")["documents"] == []
    assert a.ar_aging(conn, as_of="2026-12-31")["by_customer"] == []


def test_ap_aging_groups_by_supplier():
    conn = books("ap")
    one = a.add_supplier(conn, "s1", "Dockside Roastery")
    two = a.add_supplier(conn, "s2", "Marsh Lane Properties")
    for n, (supplier, cents, due) in enumerate([(one, 10_000, "2026-05-01"), (one, 20_000, "2026-06-20"),
                                                (two, 30_000, "2026-06-20")], start=1):
        draft = a.create_bill(conn, f"bill:{n}", supplier["id"], f"B-{n}", "2026-04-01", [
            {"description": "Supplies", "unit_price_cents": cents, "account_code": "5000"}], due_date=due)
        a.receive_bill(conn, draft["bill_id"], f"receive:{n}")
    # On 2026-06-15: B-1 is 45 days past its due date, the other two aren't due yet.
    report = a.ap_aging(conn, as_of="2026-06-15")
    assert [(row["supplier"], row["31_60"], row["current"], row["total_cents"])
            for row in report["by_supplier"]] == [("Dockside Roastery", 10_000, 20_000, 30_000),
                                                   ("Marsh Lane Properties", 0, 30_000, 30_000)]
    assert report["totals"]["total_cents"] == 60_000
    assert report["totals"]["31_60"] == 10_000 and report["totals"]["current"] == 50_000
    assert report["totals"]["1_30"] == 0
    assert [d["bill_number"] for d in report["documents"]] == ["B-1", "B-2", "B-3"]


def test_a_part_paid_document_is_aged_at_what_is_left():
    conn = books("aging_part")
    supplier = a.add_supplier(conn, "s", "Dockside Roastery")
    draft = a.create_bill(conn, "bill", supplier["id"], "DR-1", "2026-04-01", [
        {"description": "Supplies", "unit_price_cents": 50_000, "account_code": "5000"}], due_date="2026-04-30")
    a.receive_bill(conn, draft["bill_id"], "receive")
    a.record_bill_payment(conn, draft["bill_id"], 20_000, "2026-05-10", bank(conn), "pay")
    assert a.ap_aging(conn, as_of="2026-05-09")["totals"]["total_cents"] == 50_000
    report = a.ap_aging(conn, as_of="2026-05-10")
    assert report["totals"]["total_cents"] == 30_000
    assert report["documents"][0]["outstanding_cents"] == 30_000 and report["documents"][0]["days_late"] == 10
    assert report["documents"][0]["bucket"] == "1_30"


def test_the_reports_default_to_today_without_storing_it():
    """as_of is a reading date, never part of a command, so it can't change what a replay returns."""
    conn = books("today")
    import datetime
    today = datetime.date.today().isoformat()
    assert a.trial_balance(conn)["as_of"] == today
    assert a.balance_sheet(conn)["as_of"] == today
    assert a.ar_aging(conn)["as_of"] == today and a.ap_aging(conn)["as_of"] == today
    customer = a.add_customer(conn, "c", "Northgate Offices")
    invoice = a.create_invoice(conn, "inv", customer["id"], "INV-001", "2026-03-01", [
        {"description": "Catering", "unit_price_cents": 100, "account_code": "4000"}], due_date="2026-03-31")
    assert "as_of" not in conn.execute("SELECT payload FROM acct_commands WHERE idempotency_key = 'inv'"
                                       ).fetchone()["payload"]
    assert a.invoice(conn, invoice["invoice_id"])["as_of"] == today


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
