"""
Accounting module: suppliers, bills, the entries receiving and paying one
posts, voiding, and the rules that differ from the sales side. Run from the
repo root with the venv (pytest also collects these):

    python -B modules/accounting/tests/test_purchases.py
"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounting as a
from test_ledger import chart, dump, rejected

TMP = tempfile.TemporaryDirectory(prefix="accounting_purchases_", ignore_cleanup_errors=True)


def books(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = a.connect(path)
    a.init_db(conn)
    chart(conn)
    return conn, path


def bank(conn) -> int:
    return a.account_by_code(conn, "1000")["account_id"]


def a_supplier(conn, name="Dockside Roastery", key=None, **fields):
    return a.add_supplier(conn, key or f"supplier:{name}", name, **fields)


def a_bill(conn, supplier_id, number="DR-4471", key=None, issue_date="2026-03-01", lines=None, **kwargs):
    if lines is None:
        lines = [{"description": "Beans, 12 kg", "quantity": 12, "unit_price_cents": 2_250,
                  "account_code": "5000"}]
    return a.create_bill(conn, key or f"bill:{number}", supplier_id, number, issue_date, lines, **kwargs)


def received_bill(conn, supplier_id, number="DR-4471", **kwargs):
    draft = a_bill(conn, supplier_id, number, **kwargs)
    return a.receive_bill(conn, draft["bill_id"], f"receive:{number}")


# ---------------------------------------------------------------- suppliers

def test_a_supplier_is_added_and_updated_like_a_customer():
    conn, _ = books("supplier")
    supplier = a_supplier(conn, "Dockside Roastery", payment_terms_days=30, email="billing@dockside.example",
                          terms="30 days end of month", notes="Delivers Tuesdays")
    assert supplier["name"] == "Dockside Roastery" and supplier["payment_terms_days"] == 30
    assert supplier["terms"] == "30 days end of month"
    assert a.supplier(conn, supplier["id"]) == supplier and a.suppliers(conn) == [supplier]
    rejected(lambda: a_supplier(conn, " dockside   roastery ", key="dup"),
             "supplier 'dockside roastery' is already supplier 1", a.InvalidState)
    updated = a.update_supplier(conn, supplier["id"], {"payment_terms_days": 14}, "terms:14")
    assert updated["payment_terms_days"] == 14
    a.update_supplier(conn, supplier["id"], {"is_active": False}, "close")
    assert a.suppliers(conn, active_only=True) == []
    rejected(lambda: a_bill(conn, supplier["id"]), "supplier 1 is not active", a.InvalidState)
    rejected(lambda: conn.execute("DELETE FROM acct_suppliers WHERE id = 1"),
             "suppliers are kept: bills refer to them")
    rejected(lambda: a.supplier(conn, 99), "no supplier 99", a.NotFound)


def test_customers_and_suppliers_are_separate_lists():
    """One business can buy from and sell to a company with the same name without the two clashing."""
    conn, _ = books("both_sides")
    customer = a.add_customer(conn, "c", "Mirror Trading Ltd")
    supplier = a.add_supplier(conn, "s", "Mirror Trading Ltd")
    assert customer["id"] == supplier["id"] == 1                    # separate tables, separate ids
    assert [c["name"] for c in a.customers(conn)] == ["Mirror Trading Ltd"]
    assert [s["name"] for s in a.suppliers(conn)] == ["Mirror Trading Ltd"]


# ---------------------------------------------------------------- entering and receiving a bill

def test_a_bill_s_number_only_has_to_be_unique_for_its_supplier():
    """Bill numbers come from the suppliers, so two of them may well use the same one."""
    conn, _ = books("bill_numbers")
    one = a_supplier(conn, "Dockside Roastery")
    two = a_supplier(conn, "Marsh Lane Properties")
    a_bill(conn, one["id"], "0001", key="b1")
    assert a_bill(conn, two["id"], "0001", key="b2")["bill_number"] == "0001"
    rejected(lambda: a_bill(conn, one["id"], "0001", key="b3"),
             "bill number '0001' from supplier 1 is already bill 1", a.InvalidState)


def test_receiving_a_bill_debits_expenses_and_credits_payables():
    conn, _ = books("receive")
    supplier = a_supplier(conn, "Dockside Roastery", payment_terms_days=30)
    draft = a_bill(conn, supplier["id"], lines=[
        {"description": "Beans, 12 kg", "quantity": 12, "unit_price_cents": 2_250, "account_code": "5000"},
        {"description": "March rent", "unit_price_cents": 95_000, "account_code": "5000"},
        {"description": "A grinder we keep", "unit_price_cents": 40_000, "account_code": "1010"},
    ])
    assert draft["status"] == "draft" and draft["due_date"] == "2026-03-31"
    assert draft["total_cents"] == 27_000 + 95_000 + 40_000
    bill = a.receive_bill(conn, draft["bill_id"], "receive")
    assert bill["status"] == "sent" and bill["outstanding_cents"] == 162_000
    posted = a.journal_entry(conn, bill["entry_id"])
    assert posted["source"] == "bill" and posted["source_ref"] == "DR-4471"
    assert posted["memo"] == "Bill DR-4471 from Dockside Roastery"
    # The payable is the single credit; the expense and the asset are the debits, grouped by account.
    assert [(line["account_code"], line["debit_cents"], line["credit_cents"]) for line in posted["lines"]] == [
        ("2100", 0, 162_000), ("1010", 40_000, 0), ("5000", 122_000, 0)]
    assert a.account_by_code(conn, "2100")["balance_cents"] == 162_000
    assert a.account_by_code(conn, "5000")["balance_cents"] == 122_000
    assert a.trial_balance(conn, as_of="2026-03-01")["balanced"]


def test_a_bill_line_posts_to_an_expense_or_asset_account_but_never_to_a_control_account():
    conn, _ = books("bill_lines")
    supplier = a_supplier(conn, "Dockside Roastery")
    before = dump(conn)
    rejected(lambda: a_bill(conn, supplier["id"], lines=[
        {"description": "A sale", "unit_price_cents": 100, "account_code": "4000"}]),
             "is an income account; this line needs expense or asset", a.InvalidState)
    rejected(lambda: a_bill(conn, supplier["id"], lines=[
        {"description": "Owed to us", "unit_price_cents": 100, "account_code": "1100"}]),
             "is the accounts_receivable account, which an invoice or bill line never posts to", a.InvalidState)
    rejected(lambda: a_bill(conn, supplier["id"], lines=[
        {"description": "Owed by us", "unit_price_cents": 100, "account_code": "2100"}]),
             "is a liability account; this line needs expense or asset", a.InvalidState)
    assert dump(conn) == before


def test_raw_sql_can_t_post_a_bill_line_to_a_control_account():
    """The code refuses it, and so does the database on its own."""
    conn, _ = books("raw_control")
    supplier = a_supplier(conn, "Dockside Roastery")
    receivable = a.account_by_code(conn, "1100")["account_id"]
    rejected(lambda: conn.execute("""INSERT INTO acct_bill_lines (bill_id, line_no, description, quantity,
                                             unit_price_cents, amount_cents, account_id)
                                     VALUES (50, 1, 'Raw', 1, 100, 100, ?)""", (receivable,)),
             "a bill line can't post to the receivable or payable account")
    sales = a.account_by_code(conn, "4000")["account_id"]
    rejected(lambda: conn.execute("""INSERT INTO acct_bill_lines (bill_id, line_no, description, quantity,
                                             unit_price_cents, amount_cents, account_id)
                                     VALUES (50, 1, 'Raw', 1, 100, 100, ?)""", (sales,)),
             "a bill line posts to an active expense or asset account")
    assert a.bills(conn) == []
    assert supplier["id"] == 1


# ---------------------------------------------------------------- paying a bill

def test_paying_a_bill_debits_payables_and_credits_the_account_it_came_from():
    conn, _ = books("pay")
    supplier = a_supplier(conn, "Dockside Roastery")
    bill = received_bill(conn, supplier["id"])
    first = a.record_bill_payment(conn, bill["bill_id"], 7_000, "2026-03-10", bank(conn), "pay1",
                                  reference="FP 441")
    assert first["status"] == "sent" and first["outstanding_cents"] == 20_000
    second = a.record_bill_payment(conn, bill["bill_id"], 20_000, "2026-03-20", bank(conn), "pay2")
    assert second["status"] == "paid" and second["outstanding_cents"] == 0
    entries = a.journal_entries(conn, source="bill_payment")
    assert [(e["total_cents"], e["lines"][0]["account_code"], e["lines"][1]["account_code"]) for e in entries] \
        == [(7_000, "1000", "2100"), (20_000, "1000", "2100")]
    assert [(line["debit_cents"], line["credit_cents"]) for line in entries[0]["lines"]] == [(0, 7_000),
                                                                                              (7_000, 0)]
    assert a.account_by_code(conn, "2100")["balance_cents"] == 0
    assert a.account_by_code(conn, "1000")["balance_cents"] == -27_000      # the money has gone out
    assert a.trial_balance(conn, as_of="2026-03-31")["balanced"]


def test_a_bill_is_received_once_and_only_a_received_bill_is_paid():
    conn, _ = books("bill_once")
    supplier = a_supplier(conn, "Dockside Roastery")
    draft = a_bill(conn, supplier["id"])
    rejected(lambda: a.record_bill_payment(conn, draft["bill_id"], 100, "2026-03-05", bank(conn), "early"),
             "bill 1 is draft: a payment is recorded against a bill that is in the books", a.InvalidState)
    a.receive_bill(conn, draft["bill_id"], "receive")
    rejected(lambda: a.receive_bill(conn, draft["bill_id"], "receive:again"),
             "bill 1 is sent, so it is already in the books", a.InvalidState)
    rejected(lambda: a.record_bill_payment(conn, draft["bill_id"], 27_001, "2026-03-05", bank(conn), "over"),
             "bill 1 has 27000 cents outstanding, and the payment is 27001", a.InvalidState)
    rejected(lambda: a.receive_bill(conn, 99, "none"), "no bill 99", a.NotFound)


def test_money_leaves_only_an_active_asset_account_that_isn_t_receivables():
    conn, _ = books("source_account")
    supplier = a_supplier(conn, "Dockside Roastery")
    bill = received_bill(conn, supplier["id"])
    for code in ("1100", "5000", "2100"):
        rejected(lambda c=code: a.record_bill_payment(conn, bill["bill_id"], 100, "2026-03-10",
                                                      a.account_by_code(conn, c)["account_id"], f"p:{c}"),
                 "not an active asset account that money can sit in", a.InvalidState)
    assert a.record_bill_payment(conn, bill["bill_id"], 100, "2026-03-10",
                                 a.account_by_code(conn, "1010")["account_id"],
                                 "p:petty")["amount_paid_cents"] == 100


def test_voiding_a_bill_reverses_its_entry_and_a_paid_one_is_not_voided():
    conn, _ = books("void_bill")
    supplier = a_supplier(conn, "Dockside Roastery")
    bill = received_bill(conn, supplier["id"])
    duplicate = received_bill(conn, supplier["id"], "DR-4471-DUP")
    assert a.account_by_code(conn, "2100")["balance_cents"] == 54_000
    voided = a.void_bill(conn, duplicate["bill_id"], "entered twice", "void")
    assert voided["status"] == "void" and voided["void_entry_id"]
    assert a.journal_entry(conn, voided["void_entry_id"])["memo"] == "Void of bill DR-4471-DUP: entered twice"
    assert a.account_by_code(conn, "2100")["balance_cents"] == 27_000
    assert a.account_by_code(conn, "5000")["balance_cents"] == 27_000
    a.record_bill_payment(conn, bill["bill_id"], 1, "2026-03-10", bank(conn), "pay")
    rejected(lambda: a.void_bill(conn, bill["bill_id"], "too late", "void:paid"),
             "has 1 cents paid against it", a.InvalidState)
    rejected(lambda: conn.execute("UPDATE acct_bills SET status = 'void' WHERE id = 1"),
             "a bill with payments against it is not voided")


def test_voiding_a_draft_bill_writes_no_entry():
    conn, _ = books("void_draft_bill")
    supplier = a_supplier(conn, "Dockside Roastery")
    draft = a_bill(conn, supplier["id"])
    voided = a.void_bill(conn, draft["bill_id"], "wrong supplier", "void")
    assert voided["status"] == "void" and voided["entry_id"] is None and voided["void_entry_id"] is None
    assert a.journal_entries(conn) == []


def test_bills_are_listed_by_due_date_and_overdue_is_derived():
    conn, _ = books("bill_list")
    one = a_supplier(conn, "Dockside Roastery", payment_terms_days=30)
    two = a_supplier(conn, "Marsh Lane Properties", payment_terms_days=7)
    received_bill(conn, one["id"], "DR-1")
    received_bill(conn, two["id"], "MLP-1")
    a_bill(conn, one["id"], "DR-2", key="draft")
    # Due 2026-03-08 for the 7-day supplier, 2026-03-31 for the 30-day one; ties break on the id.
    assert [b["bill_number"] for b in a.bills(conn)] == ["MLP-1", "DR-1", "DR-2"]
    assert [b["bill_number"] for b in a.bills(conn, supplier_id=two["id"])] == ["MLP-1"]
    assert [b["bill_number"] for b in a.bills(conn, status="draft")] == ["DR-2"]
    assert [b["bill_number"] for b in a.bills(conn, status="overdue", as_of="2026-03-15")] == ["MLP-1"]
    assert [b["bill_number"] for b in a.bills(conn, status="overdue", as_of="2026-04-15")] == ["MLP-1", "DR-1"]
    assert [b["effective_status"] for b in a.bills(conn, as_of="2026-03-02")] == ["sent", "sent", "draft"]
    assert a.bills(conn, supplier_id=99) == []


def test_raw_sql_can_t_fake_a_bill_s_entry_or_its_payment():
    conn, _ = books("raw_bill")
    supplier = a_supplier(conn, "Dockside Roastery")
    draft = a_bill(conn, supplier["id"])
    unrelated = a.post_journal_entry(conn, "manual", "2026-03-01", "Nothing to do with the bill", [
        {"account_code": "5000", "debit_cents": 27_000}, {"account_code": "1000", "credit_cents": 27_000}])
    rejected(lambda: conn.execute("UPDATE acct_bills SET entry_id = ?, status = 'sent' WHERE id = 1",
                                  (unrelated["entry_id"],)),
             "a bill's entry must credit payables by its total")
    bill = a.receive_bill(conn, draft["bill_id"], "receive")
    rejected(lambda: conn.execute("""INSERT INTO acct_bill_payments (bill_id, payment_date, amount_cents,
                                             paid_from_account_id, entry_id, idempotency_key)
                                     VALUES (1, '2026-03-10', 5000, ?, ?, 'raw')""",
                                  (bank(conn), unrelated["entry_id"])),
             "a payment's entry must debit payables and credit the account it came from by its amount")
    rejected(lambda: conn.execute("UPDATE acct_bills SET status = 'void', void_entry_id = ? WHERE id = 1",
                                  (unrelated["entry_id"],)),
             "a void must reverse the bill's own entry")
    rejected(lambda: conn.execute("UPDATE acct_bills SET amount_paid_cents = 100 WHERE id = 1"),
             "a bill's amount paid is the sum of its payments, and never falls")
    rejected(lambda: conn.execute("UPDATE acct_bills SET status = 'draft' WHERE id = 1"),
             "a bill goes draft -> sent -> paid, or to void, and never back")
    rejected(lambda: conn.execute("DELETE FROM acct_bills WHERE id = 1"), "bills are kept")
    rejected(lambda: conn.execute("UPDATE acct_bill_lines SET amount_cents = 1 WHERE bill_id = 1"),
             "bill lines are fixed")
    assert a.bill(conn, bill["bill_id"])["status"] == "sent"


def test_a_bill_can_t_be_entered_already_sent_or_paid():
    conn, _ = books("raw_born")
    supplier = a_supplier(conn, "Dockside Roastery")
    expense = a.account_by_code(conn, "5000")["account_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""INSERT INTO acct_bill_lines (bill_id, line_no, description, quantity,
                                unit_price_cents, amount_cents, account_id)
                        VALUES (50, 1, 'Raw', 1, 1000, 1000, ?)""", (expense,))
        for status, paid in (("sent", 0), ("paid", 1000), ("void", 0)):
            rejected(lambda s=status, p=paid: conn.execute(
                """INSERT INTO acct_bills (id, supplier_id, bill_number, issue_date, due_date, status,
                           total_cents, amount_paid_cents, idempotency_key)
                   VALUES (50, ?, 'RAW-1', '2026-03-01', '2026-03-10', ?, 1000, ?, 'raw')""",
                (supplier["id"], s, p)),
                "a bill is entered as an unpaid draft")
    finally:
        conn.execute("ROLLBACK")
    assert a.bills(conn) == []


def test_replaying_a_bill_command_changes_nothing():
    conn, _ = books("replay_bill")
    supplier = a_supplier(conn, "Dockside Roastery")
    draft = a_bill(conn, supplier["id"])
    first = a.receive_bill(conn, draft["bill_id"], "receive")
    assert a.receive_bill(conn, draft["bill_id"], "receive") == first
    paid = a.record_bill_payment(conn, draft["bill_id"], 27_000, "2026-03-10", bank(conn), "pay")
    after = dump(conn)
    assert a.record_bill_payment(conn, draft["bill_id"], 27_000, "2026-03-10", bank(conn), "pay") == paid
    assert dump(conn) == after
    voided = a.void_bill(conn, a_bill(conn, supplier["id"], "DR-2", key="b2")["bill_id"], "mistake", "void")
    assert a.void_bill(conn, voided["bill_id"], "mistake", "void") == voided
    assert len(a.journal_entries(conn)) == 2


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
