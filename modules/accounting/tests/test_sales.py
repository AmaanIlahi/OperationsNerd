"""
Accounting module: customers, invoices, the entries sending and paying one
posts, voiding, and the database rules behind them. "Raw" tests write SQL
directly, bypassing accounting.py. Run from the repo root with the venv (pytest
also collects these):

    python -B modules/accounting/tests/test_sales.py
"""

import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a
from test_ledger import CHART, chart, dump, rejected

TMP = tempfile.TemporaryDirectory(prefix="accounting_sales_", ignore_cleanup_errors=True)


def books(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = a.connect(path)
    a.init_db(conn)
    chart(conn)
    return conn, path


def bank(conn) -> int:
    return a.account_by_code(conn, "1000")["account_id"]


def a_customer(conn, name="Northgate Offices", key=None, **fields):
    return a.add_customer(conn, key or f"customer:{name}", name, **fields)


def an_invoice(conn, customer_id, number="INV-001", key=None, issue_date="2026-03-01", lines=None, **kwargs):
    if lines is None:
        lines = [{"description": "Catering", "quantity": 2, "unit_price_cents": 15_000, "account_code": "4000"}]
    return a.create_invoice(conn, key or f"invoice:{number}", customer_id, number, issue_date, lines, **kwargs)


def sent_invoice(conn, customer_id, number="INV-001", **kwargs):
    draft = an_invoice(conn, customer_id, number, **kwargs)
    return a.send_invoice(conn, draft["invoice_id"], f"send:{number}")


# ---------------------------------------------------------------- customers

def test_a_customer_is_added_with_terms_and_read_back():
    conn, _ = books("customer")
    customer = a_customer(conn, "  Northgate   Offices ", payment_terms_days=14, email="AP@Northgate.Example",
                          phone="+44 20 7000 0000", billing_address="1 Northgate, Leeds",
                          terms="14 days from invoice; cancellations 48 hours ahead",
                          notes="Always asks for a PO number")
    assert customer["name"] == "Northgate Offices"                      # trimmed and collapsed
    assert customer["email"] == "ap@northgate.example"                  # lowercased
    assert customer["payment_terms_days"] == 14 and customer["is_active"]
    assert customer["terms"].startswith("14 days from invoice")
    assert a.customer(conn, customer["id"]) == customer
    assert a.customers(conn) == [customer]
    rejected(lambda: a.customer(conn, 99), "no customer 99", a.NotFound)


def test_a_customer_name_is_claimed_once_and_a_bad_email_is_refused():
    conn, _ = books("customer_unique")
    a_customer(conn, "Northgate Offices")
    rejected(lambda: a_customer(conn, " northgate offices ", key="dup"),
             "customer 'northgate offices' is already customer 1", a.InvalidState)
    rejected(lambda: a_customer(conn, "Other", key="bad:email", email="not-an-address"),
             "email must look like name@domain", ValueError)
    rejected(lambda: a_customer(conn, "Other", key="bad:terms", payment_terms_days=-1),
             "payment_terms_days must be a whole number of days", ValueError)
    rejected(lambda: a_customer(conn, "Other", key="bad:terms2", payment_terms_days=30.0),
             "payment_terms_days must be a whole number of days", ValueError)
    rejected(lambda: a_customer(conn, "Other", key="bad:field", vat_number="GB123"),
             "unknown customer fields: vat_number", ValueError)
    rejected(lambda: a.add_customer(conn, "blank", "   "), "must be a non-empty string", ValueError)


def test_a_customer_is_updated_and_can_be_closed_but_not_deleted():
    conn, path = books("customer_update")
    customer = a_customer(conn, "Northgate Offices")
    updated = a.update_customer(conn, customer["id"], {"payment_terms_days": 30, "notes": "Moved to 30 days"},
                                "terms:30")
    assert updated["payment_terms_days"] == 30 and updated["notes"] == "Moved to 30 days"
    rejected(lambda: a.update_customer(conn, customer["id"], {"payment_terms_days": 30}, "again"),
             "nothing changes for customer", ValueError)
    rejected(lambda: a.update_customer(conn, customer["id"], {"id": 5}, "bad"), "can't be updated", ValueError)
    closed = a.update_customer(conn, customer["id"], {"is_active": False}, "close")
    assert closed["is_active"] is False
    assert a.customers(conn, active_only=True) == []
    rejected(lambda: an_invoice(conn, customer["id"]), "customer 1 is not active", a.InvalidState)
    rejected(lambda: conn.execute("DELETE FROM acct_customers WHERE id = 1"),
             "customers are kept: invoices refer to them")
    rejected(lambda: conn.execute("UPDATE acct_customers SET id = 7 WHERE id = 1"),
             "a customer's id and key never change")


# ---------------------------------------------------------------- raising an invoice

def test_a_draft_invoice_totals_its_lines_and_posts_nothing():
    conn, _ = books("draft")
    customer = a_customer(conn, "Northgate Offices", payment_terms_days=14)
    invoice = an_invoice(conn, customer["id"], lines=[
        {"description": "Breakfast catering", "quantity": 3, "unit_price_cents": 18_000, "account_code": "4000"},
        {"description": "Birthday cake", "unit_price_cents": 4_500, "account_code": "4000"},
    ], terms="Payment within 14 days", notes="PO 8812")
    assert invoice["status"] == "draft" and invoice["effective_status"] == "draft"
    assert invoice["total_cents"] == 3 * 18_000 + 4_500 == 58_500
    assert invoice["due_date"] == "2026-03-15"                           # issue date plus the customer's terms
    assert invoice["entry_id"] is None and invoice["amount_paid_cents"] == 0
    assert invoice["outstanding_cents"] == 0                             # not in the books, so nothing is owed yet
    assert [(line["line_no"], line["quantity"], line["amount_cents"]) for line in invoice["lines"]] == [
        (1, 3, 54_000), (2, 1, 4_500)]
    assert invoice["terms"] == "Payment within 14 days" and invoice["notes"] == "PO 8812"
    assert a.journal_entries(conn) == []
    assert a.account_by_code(conn, "1100")["balance_cents"] == 0


def test_a_due_date_is_given_or_derived_and_never_before_the_issue_date():
    conn, _ = books("due")
    customer = a_customer(conn, "Northgate Offices", payment_terms_days=30)
    assert an_invoice(conn, customer["id"], "INV-A")["due_date"] == "2026-03-31"
    assert an_invoice(conn, customer["id"], "INV-B", due_date="2026-03-05")["due_date"] == "2026-03-05"
    assert an_invoice(conn, customer["id"], "INV-C", due_date="2026-03-01")["due_date"] == "2026-03-01"
    rejected(lambda: an_invoice(conn, customer["id"], "INV-D", due_date="2026-02-28"),
             "due_date 2026-02-28 is before the issue date 2026-03-01", ValueError)
    rejected(lambda: an_invoice(conn, customer["id"], "INV-E", due_date="2026-02-30"),
             "must be a date as 'YYYY-MM-DD'", ValueError)


def test_an_invoice_needs_lines_on_income_accounts_and_whole_quantities():
    conn, _ = books("invoice_lines")
    customer = a_customer(conn, "Northgate Offices")
    before = dump(conn)
    rejected(lambda: an_invoice(conn, customer["id"], lines=[]), "needs at least one line", ValueError)
    rejected(lambda: an_invoice(conn, customer["id"], lines=[
        {"description": "Rent", "unit_price_cents": 100, "account_code": "5000"}]),
             "is an expense account; this line needs income", a.InvalidState)
    # The receivable account is where the invoice itself lands, and it isn't income either way.
    rejected(lambda: an_invoice(conn, customer["id"], lines=[
        {"description": "Owed", "unit_price_cents": 100, "account_code": "1100"}]),
             "is an asset account; this line needs income", a.InvalidState)
    for quantity in (0, -1, 1.5, "2", True, a.MAX_QUANTITY + 1):
        rejected(lambda q=quantity: an_invoice(conn, customer["id"], lines=[
            {"description": "Catering", "quantity": q, "unit_price_cents": 100, "account_code": "4000"}]),
                 "quantity must be a whole number of units", ValueError)
    rejected(lambda: an_invoice(conn, customer["id"], lines=[
        {"description": "Catering", "unit_price_cents": 10.5, "account_code": "4000"}]),
             "must be a whole number of cents", ValueError)
    # Every line at zero would make an invoice for nothing.
    rejected(lambda: an_invoice(conn, customer["id"], lines=[
        {"description": "Free sample", "unit_price_cents": 0, "account_code": "4000"}]),
             "must come to more than nothing", ValueError)
    rejected(lambda: an_invoice(conn, customer["id"], lines=[
        {"description": "Catering", "unit_price_cents": 100, "account_code": "9999"}]),
             "no account with code '9999'", a.NotFound)
    rejected(lambda: an_invoice(conn, 99), "no customer 99", a.NotFound)
    assert dump(conn) == before
    # A zero line is fine as long as something on the invoice is not zero.
    invoice = an_invoice(conn, customer["id"], lines=[
        {"description": "Catering", "unit_price_cents": 10_000, "account_code": "4000"},
        {"description": "Delivery (waived)", "unit_price_cents": 0, "account_code": "4000"}])
    assert invoice["total_cents"] == 10_000


def test_an_invoice_number_is_claimed_once():
    conn, _ = books("numbers")
    customer = a_customer(conn, "Northgate Offices")
    an_invoice(conn, customer["id"], "INV-001")
    rejected(lambda: an_invoice(conn, customer["id"], "INV-001", key="dup"),
             "invoice number 'INV-001' is already invoice 1", a.InvalidState)
    assert an_invoice(conn, customer["id"], "INV-002")["invoice_number"] == "INV-002"


# ---------------------------------------------------------------- sending and paying

def test_sending_an_invoice_debits_receivables_and_credits_income():
    conn, _ = books("send")
    customer = a_customer(conn, "Northgate Offices", payment_terms_days=14)
    a.add_account(conn, "other:income", "4010", "Counter sales", "income")
    draft = an_invoice(conn, customer["id"], lines=[
        {"description": "Catering", "quantity": 2, "unit_price_cents": 15_000, "account_code": "4000"},
        {"description": "More catering", "unit_price_cents": 5_000, "account_code": "4000"},
        {"description": "A coffee", "unit_price_cents": 300, "account_code": "4010"},
    ])
    invoice = a.send_invoice(conn, draft["invoice_id"], "send")
    assert invoice["status"] == "sent" and invoice["outstanding_cents"] == 35_300
    posted = a.journal_entry(conn, invoice["entry_id"])
    assert posted["entry_date"] == "2026-03-01" and posted["source"] == "invoice"
    assert posted["source_ref"] == "INV-001" and posted["memo"] == "Invoice INV-001 to Northgate Offices"
    # One line per account, not per invoice line: the two catering lines become one credit.
    assert [(line["account_code"], line["debit_cents"], line["credit_cents"]) for line in posted["lines"]] == [
        ("1100", 35_300, 0), ("4000", 0, 35_000), ("4010", 0, 300)]
    assert a.account_by_code(conn, "1100")["balance_cents"] == 35_300
    assert a.account_by_code(conn, "4000")["balance_cents"] == 35_000
    assert a.trial_balance(conn, as_of="2026-03-01")["balanced"]


def test_the_entry_date_of_a_send_can_differ_from_the_issue_date():
    conn, _ = books("send_date")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"], issue_date="2026-03-01")
    invoice = a.send_invoice(conn, draft["invoice_id"], "send", entry_date="2026-04-01")
    assert a.journal_entry(conn, invoice["entry_id"])["entry_date"] == "2026-04-01"
    assert a.account_balance(conn, a.account_by_code(conn, "1100")["account_id"],
                            as_of="2026-03-31")["balance_cents"] == 0


def test_an_invoice_is_sent_once_and_only_a_sent_invoice_is_paid():
    conn, _ = books("once")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"])
    rejected(lambda: a.record_invoice_payment(conn, draft["invoice_id"], 100, "2026-03-05", bank(conn), "early"),
             "invoice 1 is draft", a.InvalidState)
    invoice = a.send_invoice(conn, draft["invoice_id"], "send")
    rejected(lambda: a.send_invoice(conn, draft["invoice_id"], "send:again"),
             "invoice 1 is sent, so it is already in the books", a.InvalidState)
    a.record_invoice_payment(conn, invoice["invoice_id"], 30_000, "2026-03-20", bank(conn), "pay")
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 1, "2026-03-21", bank(conn), "extra"),
             "invoice 1 is paid", a.InvalidState)
    rejected(lambda: a.send_invoice(conn, 99, "send:none"), "no invoice 99", a.NotFound)


def test_a_part_payment_leaves_the_invoice_sent_and_the_last_cent_settles_it():
    conn, _ = books("part")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    assert invoice["total_cents"] == 30_000
    first = a.record_invoice_payment(conn, invoice["invoice_id"], 10_000, "2026-03-10", bank(conn), "pay1",
                                     reference="BACS 1")
    assert first["status"] == "sent" and first["amount_paid_cents"] == 10_000
    assert first["outstanding_cents"] == 20_000
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 20_001, "2026-03-11", bank(conn),
                                              "too much"),
             "invoice 1 has 20000 cents outstanding, and the receipt is 20001", a.InvalidState)
    second = a.record_invoice_payment(conn, invoice["invoice_id"], 20_000, "2026-03-12", bank(conn), "pay2")
    assert second["status"] == "paid" and second["effective_status"] == "paid"
    assert second["outstanding_cents"] == 0
    assert [(p["amount_cents"], p["reference"]) for p in second["payments"]] == [(10_000, "BACS 1"),
                                                                                 (20_000, None)]
    # Each receipt is its own entry: debit the bank, credit receivables.
    entries = a.journal_entries(conn, source="invoice_payment")
    assert [(e["total_cents"], e["lines"][0]["account_code"], e["lines"][1]["account_code"]) for e in entries] \
        == [(10_000, "1000", "1100"), (20_000, "1000", "1100")]
    assert a.account_by_code(conn, "1100")["balance_cents"] == 0
    assert a.account_by_code(conn, "1000")["balance_cents"] == 30_000
    assert a.trial_balance(conn, as_of="2026-03-31")["balanced"]


def test_money_arrives_only_in_an_active_asset_account_that_isn_t_receivables():
    conn, _ = books("deposit")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    for code, why in (("1100", "not an active asset account"), ("4000", "not an active asset account"),
                      ("2100", "not an active asset account")):
        rejected(lambda c=code: a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-03-10",
                                                         a.account_by_code(conn, c)["account_id"], f"pay:{c}"),
                 why, a.InvalidState)
    petty = a.account_by_code(conn, "1010")["account_id"]
    a.update_account(conn, petty, {"is_active": False}, "close")
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-03-10", petty, "pay:closed"),
             "not an active asset account", a.InvalidState)
    a.update_account(conn, petty, {"is_active": True}, "reopen")
    assert a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-03-10", petty,
                                    "pay:petty")["amount_paid_cents"] == 100


def test_a_payment_is_whole_cents_on_a_real_date():
    conn, _ = books("payment_input")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    for amount in (0, -1, 10.5, "100", True):
        rejected(lambda v=amount: a.record_invoice_payment(conn, invoice["invoice_id"], v, "2026-03-10",
                                                           bank(conn), f"p:{v}"),
                 "amount_cents must be", ValueError)
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-02-30", bank(conn), "p:d"),
             "payment_date must be a date as 'YYYY-MM-DD'", ValueError)


# ---------------------------------------------------------------- voiding

def test_voiding_a_sent_invoice_reverses_its_entry_and_leaves_the_books_where_they_were():
    conn, _ = books("void")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    assert a.account_by_code(conn, "1100")["balance_cents"] == 30_000
    voided = a.void_invoice(conn, invoice["invoice_id"], "raised against the wrong customer", "void")
    assert voided["status"] == "void" and voided["effective_status"] == "void"
    assert voided["outstanding_cents"] == 0 and voided["void_entry_id"]
    reversal = a.journal_entry(conn, voided["void_entry_id"])
    assert reversal["reverses_id"] == invoice["entry_id"]
    assert reversal["memo"] == "Void of invoice INV-001: raised against the wrong customer"
    assert a.account_by_code(conn, "1100")["balance_cents"] == 0
    assert a.account_by_code(conn, "4000")["balance_cents"] == 0
    assert len(a.journal_entries(conn)) == 2                       # both entries stay on file
    rejected(lambda: a.void_invoice(conn, invoice["invoice_id"], "again", "void:again"),
             "invoice 1 is already void", a.InvalidState)
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-04-01", bank(conn), "pay"),
             "invoice 1 is void", a.InvalidState)


def test_voiding_a_draft_invoice_writes_no_entry():
    conn, _ = books("void_draft")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"])
    voided = a.void_invoice(conn, draft["invoice_id"], "customer changed their mind", "void")
    assert voided["status"] == "void" and voided["entry_id"] is None and voided["void_entry_id"] is None
    assert a.journal_entries(conn) == []
    rejected(lambda: a.send_invoice(conn, draft["invoice_id"], "send"),
             "invoice 1 is void", a.InvalidState)


def test_an_invoice_with_money_against_it_is_not_voided():
    conn, _ = books("void_paid")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    a.record_invoice_payment(conn, invoice["invoice_id"], 1, "2026-03-10", bank(conn), "pay")
    rejected(lambda: a.void_invoice(conn, invoice["invoice_id"], "mistake", "void"),
             "has 1 cents paid against it", a.InvalidState)
    assert a.invoice(conn, invoice["invoice_id"])["status"] == "sent"


# ---------------------------------------------------------------- statuses and listing

def test_overdue_is_worked_out_from_the_due_date_and_never_stored():
    conn, _ = books("overdue")
    customer = a_customer(conn, "Northgate Offices", payment_terms_days=14)
    invoice = sent_invoice(conn, customer["id"])
    assert invoice["due_date"] == "2026-03-15"
    assert a.invoice(conn, invoice["invoice_id"], as_of="2026-03-15")["effective_status"] == "sent"
    assert a.invoice(conn, invoice["invoice_id"], as_of="2026-03-16")["effective_status"] == "overdue"
    # Nothing in the row changed: the stored status is still 'sent'.
    assert conn.execute("SELECT status FROM acct_invoices WHERE id = 1").fetchone()[0] == "sent"
    a.record_invoice_payment(conn, invoice["invoice_id"], 30_000, "2026-04-01", bank(conn), "pay")
    assert a.invoice(conn, invoice["invoice_id"], as_of="2026-12-31")["effective_status"] == "paid"


def test_invoices_are_listed_by_due_date_and_filtered():
    conn, _ = books("list")
    one = a_customer(conn, "Northgate Offices")
    two = a_customer(conn, "Southbank Studios")
    sent_invoice(conn, one["id"], "INV-001", due_date="2026-04-30")
    sent_invoice(conn, one["id"], "INV-002", due_date="2026-03-10")
    an_invoice(conn, two["id"], "INV-003", due_date="2026-03-20")
    assert [i["invoice_number"] for i in a.invoices(conn)] == ["INV-002", "INV-003", "INV-001"]
    assert [i["invoice_number"] for i in a.invoices(conn, customer_id=two["id"])] == ["INV-003"]
    assert [i["invoice_number"] for i in a.invoices(conn, status="draft")] == ["INV-003"]
    assert [i["invoice_number"] for i in a.invoices(conn, open_only=True)] == ["INV-002", "INV-001"]
    assert [i["invoice_number"] for i in a.invoices(conn, status="overdue", as_of="2026-03-15")] == ["INV-002"]
    assert [i["invoice_number"] for i in a.invoices(conn, status="overdue", as_of="2026-05-01")] == ["INV-002",
                                                                                                     "INV-001"]
    assert a.invoices(conn, customer_id=99) == []
    rejected(lambda: a.invoices(conn, status="sent-ish"), "status must be one of", ValueError)


# ---------------------------------------------------------------- the database's own rules

def test_raw_sql_can_t_edit_an_invoice_or_its_lines():
    conn, _ = books("raw_invoice")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    a.record_invoice_payment(conn, invoice["invoice_id"], 10_000, "2026-03-10", bank(conn), "pay")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET total_cents = 1 WHERE id = 1"),
             "an invoice is not edited: void it and raise it again")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET due_date = '2027-01-01' WHERE id = 1"),
             "an invoice is not edited")
    rejected(lambda: conn.execute("DELETE FROM acct_invoices WHERE id = 1"), "invoices are kept")
    rejected(lambda: conn.execute("UPDATE acct_invoice_lines SET amount_cents = 1 WHERE id = 1"),
             "invoice lines are fixed")
    rejected(lambda: conn.execute("DELETE FROM acct_invoice_lines WHERE id = 1"), "invoice lines are fixed")
    rejected(lambda: conn.execute("""INSERT INTO acct_invoice_lines (invoice_id, line_no, description, quantity,
                                             unit_price_cents, amount_cents, account_id)
                                     VALUES (1, 9, 'Sneaked in', 1, 100, 100, 6)"""),
             "written before the invoice itself, and never added to after")
    rejected(lambda: conn.execute("DELETE FROM acct_invoice_payments"),
             "a payment is not deleted: reverse its journal entry instead")
    rejected(lambda: conn.execute("UPDATE acct_invoice_payments SET amount_cents = 1"),
             "a payment is not edited: reverse its journal entry instead")
    assert a.invoice(conn, invoice["invoice_id"])["total_cents"] == 30_000
    assert a.invoice(conn, invoice["invoice_id"])["amount_paid_cents"] == 10_000


def test_raw_sql_can_t_write_an_invoice_whose_total_misses_its_lines():
    conn, _ = books("raw_total")
    customer = a_customer(conn, "Northgate Offices")
    sales = a.account_by_code(conn, "4000")["account_id"]

    def raw_invoice(total, lines=1, invoice_id=50, key="raw"):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for line_no in range(1, lines + 1):
                conn.execute("""INSERT INTO acct_invoice_lines (invoice_id, line_no, description, quantity,
                                        unit_price_cents, amount_cents, account_id)
                                VALUES (?, ?, 'Raw', 1, 1000, 1000, ?)""", (invoice_id, line_no, sales))
            conn.execute("""INSERT INTO acct_invoices (id, customer_id, invoice_number, issue_date, due_date,
                                    total_cents, idempotency_key)
                            VALUES (?, ?, 'RAW-1', '2026-03-01', '2026-03-10', ?, ?)""",
                         (invoice_id, customer["id"], total, key))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    rejected(lambda: raw_invoice(999), "an invoice's total must equal its lines, and it needs at least one")
    rejected(lambda: raw_invoice(1000, lines=2), "an invoice's total must equal its lines")
    rejected(lambda: raw_invoice(1000, lines=0), "needs at least one")
    # A line whose amount isn't its quantity times its unit price never gets written at all.
    rejected(lambda: conn.execute("""INSERT INTO acct_invoice_lines (invoice_id, line_no, description, quantity,
                                             unit_price_cents, amount_cents, account_id)
                                     VALUES (60, 1, 'Wrong maths', 3, 1000, 1000, ?)""", (sales,)),
             "CHECK constraint failed")
    assert a.invoices(conn) == []


def test_raw_sql_can_t_move_an_invoice_backwards_or_into_the_books_without_an_entry():
    conn, _ = books("raw_status")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"], "INV-001")
    # 'sent' without an entry breaks the CHECK that ties the two together.
    rejected(lambda: conn.execute("UPDATE acct_invoices SET status = 'sent' WHERE id = 1"),
             "CHECK constraint failed")
    invoice = a.send_invoice(conn, draft["invoice_id"], "send")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET status = 'draft' WHERE id = 1"),
             "an invoice goes draft -> sent -> paid, or to void, and never back")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET status = 'paid' WHERE id = 1"),
             "CHECK constraint failed")                       # 'paid' with nothing paid
    a.record_invoice_payment(conn, invoice["invoice_id"], 30_000, "2026-03-10", bank(conn), "pay")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET status = 'sent' WHERE id = 1"),
             "never back")
    # The paid figure is the payments, and it never falls.
    rejected(lambda: conn.execute("UPDATE acct_invoices SET amount_paid_cents = 5 WHERE id = 1"),
             "an invoice's amount paid is the sum of its receipts, and never falls")


def test_raw_sql_can_t_fake_an_invoice_s_entry_or_its_void():
    conn, _ = books("raw_entry")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"], "INV-001")
    unrelated = a.post_journal_entry(conn, "manual", "2026-03-01", "Nothing to do with the invoice", [
        {"account_code": "1000", "debit_cents": 30_000}, {"account_code": "3000", "credit_cents": 30_000}])
    rejected(lambda: conn.execute("UPDATE acct_invoices SET entry_id = ?, status = 'sent' WHERE id = 1",
                                  (unrelated["entry_id"],)),
             "an invoice's entry must debit receivables by its total")
    invoice = a.send_invoice(conn, draft["invoice_id"], "send")
    # A void has to point at a reversal of this invoice's own entry.
    rejected(lambda: conn.execute("UPDATE acct_invoices SET status = 'void', void_entry_id = ? WHERE id = 1",
                                  (unrelated["entry_id"],)),
             "a void must reverse the invoice's own entry")
    assert a.invoice(conn, invoice["invoice_id"])["status"] == "sent"


def test_raw_sql_can_t_record_a_payment_without_a_matching_entry():
    conn, _ = books("raw_payment")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    unrelated = a.post_journal_entry(conn, "manual", "2026-03-10", "Not a receipt", [
        {"account_code": "1000", "debit_cents": 5_000}, {"account_code": "3000", "credit_cents": 5_000}])
    rejected(lambda: conn.execute("""INSERT INTO acct_invoice_payments (invoice_id, payment_date, amount_cents,
                                             deposit_account_id, entry_id, idempotency_key)
                                     VALUES (1, '2026-03-10', 5000, ?, ?, 'raw')""",
                                  (bank(conn), unrelated["entry_id"])),
             "a receipt's entry must credit receivables and debit the deposit account by its amount")
    # And a payment against a draft or void invoice is refused outright.
    draft = an_invoice(conn, customer["id"], "INV-002")
    paid = a.record_invoice_payment(conn, invoice["invoice_id"], 30_000, "2026-03-11", bank(conn), "pay")
    assert paid["status"] == "paid"
    receipt = conn.execute("SELECT entry_id FROM acct_invoice_payments WHERE id = 1").fetchone()["entry_id"]
    rejected(lambda: conn.execute("""INSERT INTO acct_invoice_payments (invoice_id, payment_date, amount_cents,
                                             deposit_account_id, entry_id, idempotency_key)
                                     VALUES (?, '2026-03-10', 5000, ?, ?, 'raw2')""",
                                  (draft["invoice_id"], bank(conn), receipt)),
             "a receipt is recorded against a sent invoice")


# ---------------------------------------------------------------- idempotency on the sales side

def test_replaying_a_send_or_a_payment_changes_nothing():
    conn, _ = books("replay")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"])
    first = a.send_invoice(conn, draft["invoice_id"], "send")
    assert a.send_invoice(conn, draft["invoice_id"], "send") == first
    paid = a.record_invoice_payment(conn, draft["invoice_id"], 10_000, "2026-03-10", bank(conn), "pay")
    after = dump(conn)
    assert a.record_invoice_payment(conn, draft["invoice_id"], 10_000, "2026-03-10", bank(conn), "pay") == paid
    assert dump(conn) == after
    assert len(a.journal_entries(conn)) == 2
    assert a.invoice(conn, draft["invoice_id"])["amount_paid_cents"] == 10_000
    # The same key for a different payment is a conflict, so a retry can't quietly double-pay.
    rejected(lambda: a.record_invoice_payment(conn, draft["invoice_id"], 20_000, "2026-03-10", bank(conn), "pay"),
             "was already used for record_invoice_payment", a.IdempotencyConflict)


def _rollback(conn, fn):
    """Runs fn() in one transaction and undoes it, including when fn() itself raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        fn()
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def test_a_waived_line_on_its_own_account_posts_nothing_to_that_account():
    """A zero price is a real line on the invoice, but it is not a journal line: both amounts would be 0,
    which the journal refuses. Sending used to die there when the waived line sat on its own account."""
    conn, _ = books("waived")
    customer = a_customer(conn, "Northgate Offices")
    a.add_account(conn, "delivery", "4010", "Delivery", "income")
    draft = an_invoice(conn, customer["id"], lines=[
        {"description": "Catering", "unit_price_cents": 10_000, "account_code": "4000"},
        {"description": "Delivery (waived)", "unit_price_cents": 0, "account_code": "4010"},
    ])
    invoice = a.send_invoice(conn, draft["invoice_id"], "send")
    assert [(line["description"], line["amount_cents"]) for line in invoice["lines"]] == [
        ("Catering", 10_000), ("Delivery (waived)", 0)]
    posted = a.journal_entry(conn, invoice["entry_id"])
    assert [(line["account_code"], line["debit_cents"], line["credit_cents"]) for line in posted["lines"]] == [
        ("1100", 10_000, 0), ("4000", 0, 10_000)]
    assert a.account_by_code(conn, "4010")["balance_cents"] == 0
    assert a.trial_balance(conn, as_of="2026-03-01")["balanced"]
    receivable = a.account_by_code(conn, "1100")["account_id"]
    assert a.ar_aging(conn, as_of="2026-03-01")["totals"]["total_cents"] == a.account_balance(
        conn, receivable, as_of="2026-03-01")["balance_cents"] == 10_000


def test_a_payment_is_not_dated_before_the_documents_entry():
    """A receipt dated before the invoice's entry credits receivables on a day the invoice is not in the
    aging, so the two stop agreeing. The same day as the entry is the earliest that keeps them equal."""
    conn, _ = books("pay_date")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"], issue_date="2026-03-01")
    invoice = a.send_invoice(conn, draft["invoice_id"], "send", entry_date="2026-04-01")
    rejected(lambda: a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-03-15", bank(conn),
                                              "early"),
             "payment_date 2026-03-15 is before the entry date 2026-04-01", ValueError)
    assert a.invoice(conn, invoice["invoice_id"])["amount_paid_cents"] == 0
    paid = a.record_invoice_payment(conn, invoice["invoice_id"], 100, "2026-04-01", bank(conn), "ok")
    assert paid["amount_paid_cents"] == 100
    bank_id = bank(conn)
    ar = a.account_by_code(conn, "1100")["account_id"]

    def early_receipt():
        def work():
            entry_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM acct_journal_entries").fetchone()[0]
            conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents, credit_cents)
                            VALUES (?, 1, ?, 100, 0), (?, 2, ?, 0, 100)""",
                         (entry_id, bank_id, entry_id, ar))
            conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source, total_cents,
                                                              idempotency_key)
                            VALUES (?, '2026-03-15', 'Early receipt', 'invoice_payment', 100, 'raw-entry')""",
                         (entry_id,))
            conn.execute("""INSERT INTO acct_invoice_payments (invoice_id, payment_date, amount_cents,
                                    deposit_account_id, entry_id, idempotency_key)
                            VALUES (?, '2026-03-15', 100, ?, ?, 'raw-pay')""",
                         (invoice["invoice_id"], bank_id, entry_id))
        _rollback(conn, work)

    rejected(early_receipt, "a receipt is dated on or after the invoice's entry")
    assert a.invoice(conn, invoice["invoice_id"])["amount_paid_cents"] == 100
    for day in ("2026-03-31", "2026-04-01"):
        assert a.ar_aging(conn, as_of=day)["totals"]["total_cents"] == a.account_balance(
            conn, ar, as_of=day)["balance_cents"], day


def test_a_void_is_not_dated_before_the_documents_entry():
    """A void dated first puts the reversal in the books before the invoice, so receivables go the wrong way
    on the days in between and the aging, which skips the invoice, no longer matches them."""
    conn, _ = books("void_date")
    customer = a_customer(conn, "Northgate Offices")
    draft = an_invoice(conn, customer["id"], issue_date="2026-03-01")
    invoice = a.send_invoice(conn, draft["invoice_id"], "send", entry_date="2026-04-01")
    rejected(lambda: a.void_invoice(conn, invoice["invoice_id"], "wrong customer", "void",
                                    entry_date="2026-03-15"),
             "entry_date 2026-03-15 is before the entry date 2026-04-01", ValueError)

    def early_void():
        def work():
            original = invoice["entry_id"]
            lines = conn.execute("""SELECT line_no, account_id, debit_cents, credit_cents
                                    FROM acct_journal_lines WHERE entry_id = ? ORDER BY line_no""",
                                 (original,)).fetchall()
            reversal_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM acct_journal_entries").fetchone()[0]
            for line in lines:
                conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents,
                                                                credit_cents)
                                VALUES (?, ?, ?, ?, ?)""",
                             (reversal_id, line["line_no"], line["account_id"], line["credit_cents"],
                              line["debit_cents"]))
            conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source, total_cents,
                                                              reverses_id, idempotency_key)
                            VALUES (?, '2026-03-15', 'Early void', 'invoice', ?, ?, 'raw-void')""",
                         (reversal_id, invoice["total_cents"], original))
            conn.execute("UPDATE acct_invoices SET status = 'void', void_entry_id = ? WHERE id = ?",
                         (reversal_id, invoice["invoice_id"]))
        _rollback(conn, work)

    rejected(early_void, "a void is dated on or after the invoice's entry")
    assert a.invoice(conn, invoice["invoice_id"])["status"] == "sent"
    assert len(a.journal_entries(conn)) == 1
    voided = a.void_invoice(conn, invoice["invoice_id"], "wrong customer", "void:ok")
    assert a.journal_entry(conn, voided["void_entry_id"])["entry_date"] == "2026-04-01"
    receivable = a.account_by_code(conn, "1100")["account_id"]
    for day in ("2026-03-31", "2026-04-01"):
        assert a.ar_aging(conn, as_of=day)["totals"]["total_cents"] == a.account_balance(
            conn, receivable, as_of=day)["balance_cents"] == 0, day


def test_a_document_entry_or_a_payment_entry_is_not_reversed_on_its_own():
    """Reversing the invoice's entry, or the receipt's, takes the figure out of the journal and leaves the
    document still counting it, so the aging and the receivable account disagree. Void is what reverses an
    invoice; a receipt is not reversed at all."""
    conn, _ = books("no_reverse_doc")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    rejected(lambda: a.reverse_journal_entry(conn, invoice["entry_id"], "rev:inv"),
             "was posted by invoice; only a manual or payroll entry can be reversed here", a.InvalidState)
    paid = a.record_invoice_payment(conn, invoice["invoice_id"], 10_000, "2026-03-10", bank(conn), "pay")
    payment_entry = paid["payments"][0]["entry_id"]
    rejected(lambda: a.reverse_journal_entry(conn, payment_entry, "rev:pay"),
             "was posted by invoice_payment; only a manual or payroll entry can be reversed here",
             a.InvalidState)

    def reverse_payment():
        def work():
            lines = conn.execute("""SELECT line_no, account_id, debit_cents, credit_cents
                                    FROM acct_journal_lines WHERE entry_id = ? ORDER BY line_no""",
                                 (payment_entry,)).fetchall()
            reversal_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM acct_journal_entries").fetchone()[0]
            for line in lines:
                conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents,
                                                                credit_cents)
                                VALUES (?, ?, ?, ?, ?)""",
                             (reversal_id, line["line_no"], line["account_id"], line["credit_cents"],
                              line["debit_cents"]))
            conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source, total_cents,
                                                              reverses_id, idempotency_key)
                            VALUES (?, '2026-03-10', 'Undo receipt', 'invoice_payment', 10000, ?, 'raw-rev')""",
                         (reversal_id, payment_entry))
        _rollback(conn, work)

    rejected(reverse_payment, "a payment entry is not reversed")
    assert a.invoice(conn, invoice["invoice_id"])["amount_paid_cents"] == 10_000
    assert a.invoice(conn, invoice["invoice_id"])["status"] == "sent"
    receivable = a.account_by_code(conn, "1100")["account_id"]
    assert a.ar_aging(conn, as_of="2026-03-31")["totals"]["total_cents"] == a.account_balance(
        conn, receivable, as_of="2026-03-31")["balance_cents"] == 20_000


def test_two_invoices_can_t_share_one_journal_entry():
    """The entry check only asks that receivables were debited by this invoice's total, so two invoices for
    the same amount would both accept one entry and the aging would count it twice."""
    conn, _ = books("shared_entry")
    customer = a_customer(conn, "Northgate Offices")
    first = sent_invoice(conn, customer["id"], "INV-001")
    second = sent_invoice(conn, customer["id"], "INV-002")
    rejected(lambda: conn.execute("UPDATE acct_invoices SET entry_id = ? WHERE id = ?",
                                  (first["entry_id"], second["invoice_id"])),
             "UNIQUE constraint failed")
    assert a.invoice(conn, second["invoice_id"])["entry_id"] == second["entry_id"]
    receivable = a.account_by_code(conn, "1100")["account_id"]
    assert a.ar_aging(conn, as_of="2026-03-31")["totals"]["total_cents"] == a.account_balance(
        conn, receivable, as_of="2026-03-31")["balance_cents"] == 60_000


def test_two_threads_paying_the_same_invoice_can_t_overpay_it():
    """Each thread pays the whole outstanding amount with its own key; one wins and the other finds the
    invoice settled."""
    conn, path = books("race_pay")
    customer = a_customer(conn, "Northgate Offices")
    invoice = sent_invoice(conn, customer["id"])
    conn.close()
    results, errors, barrier = [], [], threading.Barrier(2)

    def worker(n):
        own = a.connect(path)
        try:
            barrier.wait()
            results.append(a.record_invoice_payment(own, invoice["invoice_id"], 30_000, "2026-03-10",
                                                    bank(own), f"pay:{n}"))
        except a.InvalidState as e:
            errors.append(str(e))
        finally:
            own.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(errors) == 1, (results, errors)
    assert "is paid" in errors[0]
    conn = a.connect(path)
    assert a.invoice(conn, invoice["invoice_id"])["amount_paid_cents"] == 30_000
    assert len(a.journal_entries(conn, source="invoice_payment")) == 1
    assert a.trial_balance(conn, as_of="2026-03-31")["balanced"]


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
