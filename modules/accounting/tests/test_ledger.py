"""
Accounting module: the chart of accounts, the double-entry journal, reversals,
idempotency and the database rules behind them. Tests marked "raw" write SQL
directly, bypassing accounting.py, so whatever they see rejected is rejected by
the database itself. Each test gets its own throwaway database under a temp
directory. Run from the repo root with the venv (pytest also collects these):

    python -B modules/accounting/tests/test_ledger.py
"""

import ast
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounting as a

TMP = tempfile.TemporaryDirectory(prefix="accounting_ledger_", ignore_cleanup_errors=True)
TABLES = ("acct_commands", "acct_accounts", "acct_journal_entries", "acct_journal_lines", "acct_customers",
          "acct_suppliers", "acct_invoices", "acct_invoice_lines", "acct_invoice_payments", "acct_bills",
          "acct_bill_lines", "acct_bill_payments")

# A chart every test can lean on: (code, name, type, role).
CHART = (("1000", "Bank", "asset", None),
         ("1010", "Petty cash", "asset", None),
         ("1100", "Accounts receivable", "asset", "accounts_receivable"),
         ("2100", "Accounts payable", "liability", "accounts_payable"),
         ("3000", "Capital", "equity", None),
         ("4000", "Sales", "income", None),
         ("5000", "Rent", "expense", None))


def new_db(name: str) -> tuple[sqlite3.Connection, str]:
    path = os.path.join(TMP.name, name + ".db")
    conn = a.connect(path)
    a.init_db(conn)
    return conn, path


def with_chart(name: str) -> tuple[sqlite3.Connection, str]:
    conn, path = new_db(name)
    chart(conn)
    return conn, path


def chart(conn) -> dict:
    """The standard chart, as {code: account_id}."""
    out = {}
    for code, label, kind, role in CHART:
        out[code] = a.add_account(conn, f"chart:{code}", code, label, kind, role=role)["account_id"]
    return out


def dump(conn) -> list:
    """Every row of every accounting table, to show that something wrote nothing."""
    return [(t,) + tuple(r) for t in TABLES for r in conn.execute(f"SELECT * FROM {t} ORDER BY id")]


def rejected(fn, expected: str, error=sqlite3.IntegrityError) -> None:
    try:
        fn()
    except error as e:
        assert expected in str(e), f"rejected for the wrong reason: {e!r}"
        return
    raise AssertionError(f"expected {error.__name__} ({expected!r}), but it was accepted")


def _called_name(func) -> str | None:
    """The bare name of whatever a Call node calls: f(), module.f() or self.f()."""
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)


def entry(conn, key, *pairs, date="2026-01-05", memo="Test entry", source="manual", source_ref=None):
    """post_journal_entry from (code, 'debit'|'credit', cents) triples."""
    lines = [{"account_code": code, f"{side}_cents": cents} for code, side, cents in pairs]
    return a.post_journal_entry(conn, key, date, memo, lines, source=source, source_ref=source_ref)


# ---------------------------------------------------------------- the chart of accounts

def test_an_account_is_added_and_read_back():
    conn, _ = new_db("accounts")
    bank = a.add_account(conn, "k1", " 1000 ", "Bank  current", "asset", notes="Main account")
    assert bank["code"] == "1000" and bank["name"] == "Bank current"       # trimmed and collapsed
    assert bank["account_type"] == "asset" and bank["role"] is None and bank["is_active"]
    assert bank["debit_cents"] == bank["credit_cents"] == bank["balance_cents"] == 0
    assert a.account(conn, bank["account_id"]) == bank
    assert a.account_by_code(conn, "1000") == bank
    rejected(lambda: a.account(conn, 99), "no account 99", a.NotFound)
    rejected(lambda: a.account_by_code(conn, "9999"), "no account with code '9999'", a.NotFound)


def test_accounts_are_ordered_by_code_and_filtered_by_type():
    conn, _ = with_chart("chart")
    assert [x["code"] for x in a.accounts(conn)] == sorted(c for c, _, _, _ in CHART)
    assert [x["code"] for x in a.accounts(conn, account_type="asset")] == ["1000", "1010", "1100"]
    a.update_account(conn, a.account_by_code(conn, "1010")["account_id"], {"is_active": False}, "close")
    assert [x["code"] for x in a.accounts(conn, account_type="asset", active_only=True)] == ["1000", "1100"]
    rejected(lambda: a.accounts(conn, account_type="profit"), "account_type must be one of", ValueError)


def test_a_code_a_name_and_a_role_are_each_claimed_once():
    conn, _ = with_chart("unique")
    rejected(lambda: a.add_account(conn, "dup:code", "1000", "Second bank", "asset"),
             "account code '1000' is already account", a.InvalidState)
    rejected(lambda: a.add_account(conn, "dup:name", "1001", "  bank  ", "asset"),
             "is already account", a.InvalidState)
    rejected(lambda: a.add_account(conn, "dup:role", "1101", "Other receivable", "asset",
                                   role="accounts_receivable"),
             "is already the accounts_receivable account", a.InvalidState)
    # The role is only on the account type it can be.
    rejected(lambda: a.add_account(conn, "bad:role", "1102", "Odd", "liability", role="accounts_receivable"),
             "the accounts_receivable account is an asset", ValueError)
    rejected(lambda: a.add_account(conn, "bad:role2", "2101", "Odd", "asset", role="accounts_payable"),
             "the accounts_payable account is a liability", ValueError)
    rejected(lambda: a.add_account(conn, "bad:role3", "9000", "Odd", "asset", role="petty"),
             "role must be one of", ValueError)


def test_only_an_account_s_name_notes_and_use_change():
    conn, _ = with_chart("update")
    bank = a.account_by_code(conn, "1000")["account_id"]
    updated = a.update_account(conn, bank, {"name": "Bank current account", "notes": "Renamed"}, "rename")
    assert updated["name"] == "Bank current account" and updated["code"] == "1000"
    for field, value in (("code", "9999"), ("account_type", "expense"), ("role", "accounts_payable")):
        rejected(lambda f=field, v=value: a.update_account(conn, bank, {f: v}, f"bad:{f}"),
                 "can't be updated", ValueError)
    rejected(lambda: a.update_account(conn, bank, {"name": "Bank current account"}, "nothing"),
             "nothing changes for account", ValueError)
    rejected(lambda: a.update_account(conn, bank, {}, "empty"), "must be a non-empty dict", ValueError)
    # The receivable and payable accounts are what invoices and bills post to, so they stay in use.
    receivable = a.account_by_code(conn, "1100")["account_id"]
    rejected(lambda: a.update_account(conn, receivable, {"is_active": False}, "close:ar"),
             "invoices and bills need it", a.InvalidState)


def test_a_closed_account_keeps_its_history_but_takes_nothing_new():
    conn, _ = with_chart("closed")
    entry(conn, "e1", ("1010", "debit", 5_000), ("3000", "credit", 5_000))
    petty = a.account_by_code(conn, "1010")
    assert petty["balance_cents"] == 5_000
    a.update_account(conn, petty["account_id"], {"is_active": False}, "close")
    assert a.account_by_code(conn, "1010")["balance_cents"] == 5_000           # still there
    rejected(lambda: entry(conn, "e2", ("1010", "debit", 100), ("3000", "credit", 100)),
             "is closed, so nothing is posted to it", a.InvalidState)
    a.update_account(conn, petty["account_id"], {"is_active": True}, "reopen")
    assert entry(conn, "e3", ("1010", "debit", 100), ("3000", "credit", 100))["total_cents"] == 100


def test_raw_sql_can_t_change_a_code_or_delete_an_account():
    conn, _ = with_chart("raw_accounts")
    rejected(lambda: conn.execute("UPDATE acct_accounts SET code = '9999' WHERE code = '1000'"),
             "code, type and role never change")
    rejected(lambda: conn.execute("UPDATE acct_accounts SET account_type = 'expense' WHERE code = '1000'"),
             "code, type and role never change")
    rejected(lambda: conn.execute("DELETE FROM acct_accounts WHERE code = '1000'"),
             "accounts are kept: the journal refers to them")
    rejected(lambda: conn.execute("""INSERT INTO acct_accounts (code, name, account_type, idempotency_key)
                                     VALUES ('9000', 'Nonsense', 'profit', 'raw')"""), "CHECK constraint failed")
    rejected(lambda: conn.execute("""INSERT INTO acct_accounts (code, name, account_type, role, idempotency_key)
                                     VALUES ('9000', 'Nonsense', 'income', 'accounts_payable', 'raw')"""),
             "CHECK constraint failed")


# ---------------------------------------------------------------- the journal

def test_a_balanced_entry_is_posted_and_moves_two_accounts():
    conn, _ = with_chart("post")
    posted = entry(conn, "open", ("1000", "debit", 500_000), ("3000", "credit", 500_000),
                   date="2026-01-01", memo="Opening capital")
    assert posted["entry_id"] == 1 and posted["total_cents"] == 500_000 and posted["source"] == "manual"
    assert posted["reverses_id"] is None and posted["reversed_by_id"] is None
    assert [(line["line_no"], line["account_code"], line["debit_cents"], line["credit_cents"])
            for line in posted["lines"]] == [(1, "1000", 500_000, 0), (2, "3000", 0, 500_000)]
    assert a.account_by_code(conn, "1000")["balance_cents"] == 500_000
    # Equity is a natural credit, so its balance is positive when it is credited.
    assert a.account_by_code(conn, "3000")["balance_cents"] == 500_000
    assert a.journal_entry(conn, 1) == posted


def test_an_unbalanced_entry_is_refused_and_writes_nothing():
    conn, _ = with_chart("unbalanced")
    before = dump(conn)
    rejected(lambda: entry(conn, "bad", ("1000", "debit", 100), ("3000", "credit", 99)),
             "debits (100 cents) must equal credits (99 cents)", a.Unbalanced)
    rejected(lambda: entry(conn, "bad2", ("1000", "debit", 100), ("3000", "debit", 100)),
             "must equal credits (0 cents)", a.Unbalanced)
    assert dump(conn) == before
    # The key of a failed command is still free.
    assert entry(conn, "bad", ("1000", "debit", 100), ("3000", "credit", 100))["entry_id"] == 1


def test_an_entry_needs_two_lines_each_a_debit_or_a_credit():
    conn, _ = with_chart("lines")
    rejected(lambda: a.post_journal_entry(conn, "one", "2026-01-05", "Half an entry",
                                          [{"account_code": "1000", "debit_cents": 100}]),
             "needs at least two lines", ValueError)
    rejected(lambda: a.post_journal_entry(conn, "both", "2026-01-05", "Both sides",
                                          [{"account_code": "1000", "debit_cents": 100, "credit_cents": 100},
                                           {"account_code": "3000", "credit_cents": 100}]),
             "must be a debit or a credit, not both and not nothing", ValueError)
    rejected(lambda: a.post_journal_entry(conn, "neither", "2026-01-05", "Neither side",
                                          [{"account_code": "1000"}, {"account_code": "3000"}]),
             "must be a debit or a credit, not both and not nothing", ValueError)
    rejected(lambda: a.post_journal_entry(conn, "unknown", "2026-01-05", "Odd field",
                                          [{"account_code": "1000", "debit_cents": 1, "quantity": 2},
                                           {"account_code": "3000", "credit_cents": 1}]),
             "line 1 has unknown fields: quantity", ValueError)
    rejected(lambda: a.post_journal_entry(conn, "neither_account", "2026-01-05", "No account",
                                          [{"debit_cents": 1}, {"account_code": "3000", "credit_cents": 1}]),
             "must name its account by account_id or account_code", ValueError)
    rejected(lambda: a.post_journal_entry(conn, "both_accounts", "2026-01-05", "Two names",
                                          [{"account_id": 1, "account_code": "1000", "debit_cents": 1},
                                           {"account_code": "3000", "credit_cents": 1}]),
             "not both and not neither", ValueError)
    assert a.journal_entries(conn) == []


def test_amounts_are_whole_cents_never_floats():
    conn, _ = with_chart("cents")
    for amount in (10.5, 10.0, "10", True, -5, 0, a.MAX_CENTS + 1):
        rejected(lambda v=amount: entry(conn, f"bad:{v}", ("1000", "debit", v), ("3000", "credit", 100)),
                 "", ValueError)
    assert entry(conn, "ok", ("1000", "debit", 1), ("3000", "credit", 1))["total_cents"] == 1


def test_only_manual_and_payroll_entries_are_posted_by_hand():
    conn, _ = with_chart("sources")
    for source in ("invoice", "bill", "invoice_payment", "bill_payment", "nonsense"):
        rejected(lambda s=source: entry(conn, f"src:{s}", ("1000", "debit", 100), ("3000", "credit", 100),
                                        source=s),
                 "invoices, bills and payments post their own entries", ValueError)
    assert entry(conn, "manual", ("1000", "debit", 100), ("3000", "credit", 100))["source"] == "manual"
    payroll = entry(conn, "payroll", ("5000", "debit", 100), ("1000", "credit", 100), source="payroll",
                    source_ref="PR-01")
    assert payroll["source"] == "payroll" and payroll["source_ref"] == "PR-01"


def test_the_payroll_hook_posts_a_payroll_entry_and_nothing_calls_it():
    """post_payroll_entry is the documented, unwired hook for the HR module: it works, and no other code
    path in either module reaches it."""
    conn, _ = with_chart("payroll")
    a.add_account(conn, "wages", "5100", "Wages", "expense")
    a.add_account(conn, "paye", "2200", "PAYE payable", "liability")
    a.add_account(conn, "net", "2210", "Net wages payable", "liability")
    run = a.post_payroll_entry(conn, "pay:2026-01", "2026-01-31", "January pay run", [
        {"account_code": "5100", "debit_cents": 300_000},
        {"account_code": "2200", "credit_cents": 60_000},
        {"account_code": "2210", "credit_cents": 240_000},
    ], source_ref="PR-2026-01")
    assert run["source"] == "payroll" and run["total_cents"] == 300_000 and len(run["lines"]) == 3
    assert [e["entry_id"] for e in a.journal_entries(conn, source="payroll")] == [run["entry_id"]]
    assert a.account_by_code(conn, "5100")["balance_cents"] == 300_000
    assert a.account_by_code(conn, "2210")["balance_cents"] == 240_000
    # Paying the staff is the second entry, and the liability clears.
    a.post_payroll_entry(conn, "pay:2026-01:bank", "2026-02-01", "January net wages paid", [
        {"account_code": "2210", "debit_cents": 240_000},
        {"account_code": "1000", "credit_cents": 240_000},
    ], source_ref="PR-2026-01")
    assert a.account_by_code(conn, "2210")["balance_cents"] == 0
    # Unwired means unwired: no call to it anywhere in the module, only the definition. Parsed rather
    # than grepped, so the README-style mentions in the docstrings don't count.
    for path in (Path(a.__file__), Path(a.__file__).with_name("api.py")):
        calls = [node for node in ast.walk(ast.parse(path.read_text()))
                 if isinstance(node, ast.Call) and _called_name(node.func) == "post_payroll_entry"]
        assert calls == [], f"{path.name} now calls the payroll hook on line {calls[0].lineno}"


def test_entries_are_listed_by_date_and_filtered():
    conn, _ = with_chart("list")
    entry(conn, "e2", ("1000", "debit", 200), ("4000", "credit", 200), date="2026-02-01")
    entry(conn, "e1", ("1000", "debit", 100), ("4000", "credit", 100), date="2026-01-01")
    entry(conn, "e3", ("5000", "debit", 300), ("1000", "credit", 300), date="2026-03-01", source="payroll")
    assert [e["entry_date"] for e in a.journal_entries(conn)] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert [e["entry_date"] for e in a.journal_entries(conn, from_date="2026-02-01")] == ["2026-02-01",
                                                                                          "2026-03-01"]
    assert [e["entry_date"] for e in a.journal_entries(conn, to_date="2026-01-31")] == ["2026-01-01"]
    assert [e["entry_date"] for e in a.journal_entries(conn, source="payroll")] == ["2026-03-01"]
    assert len(a.journal_entries(conn, limit=1)) == 1
    rejected(lambda: a.journal_entries(conn, limit=0), "limit must be a positive integer", ValueError)
    rejected(lambda: a.journal_entries(conn, from_date="2026-2-1"), "must be a date as 'YYYY-MM-DD'", ValueError)


def test_a_balance_is_read_as_of_a_date():
    conn, _ = with_chart("as_of")
    bank = a.account_by_code(conn, "1000")["account_id"]
    entry(conn, "jan", ("1000", "debit", 1_000), ("4000", "credit", 1_000), date="2026-01-10")
    entry(conn, "feb", ("1000", "credit", 400), ("5000", "debit", 400), date="2026-02-10")
    assert a.account_balance(conn, bank, as_of="2026-01-09")["balance_cents"] == 0
    assert a.account_balance(conn, bank, as_of="2026-01-10")["balance_cents"] == 1_000
    assert a.account_balance(conn, bank, as_of="2026-02-10") == {
        "account_id": bank, "code": "1000", "name": "Bank", "account_type": "asset", "role": None,
        "is_active": True, "debit_cents": 1_000, "credit_cents": 400, "balance_cents": 600}
    assert a.account_balance(conn, bank)["balance_cents"] == 600                   # everything, by default
    rejected(lambda: a.account_balance(conn, bank, as_of="2026-02-30"), "must be a date as", ValueError)


# ---------------------------------------------------------------- reversals

def test_a_reversal_mirrors_the_entry_and_leaves_every_account_where_it_was():
    conn, _ = with_chart("reverse")
    original = entry(conn, "wrong", ("5000", "debit", 7_500), ("1000", "credit", 7_500), date="2026-03-01",
                     memo="Rent paid twice")
    before = {code: a.account_by_code(conn, code)["balance_cents"] for code, _, _, _ in CHART}
    reversal = a.reverse_journal_entry(conn, original["entry_id"], "fix")
    assert reversal["reverses_id"] == original["entry_id"] and reversal["entry_date"] == "2026-03-01"
    assert reversal["memo"] == f"Reversal of entry {original['entry_id']}: Rent paid twice"
    assert [(line["account_code"], line["debit_cents"], line["credit_cents"]) for line in reversal["lines"]] == [
        ("5000", 0, 7_500), ("1000", 7_500, 0)]
    after = {code: a.account_by_code(conn, code)["balance_cents"] for code, _, _, _ in CHART}
    assert after == {code: 0 for code in before}
    assert a.journal_entry(conn, original["entry_id"])["reversed_by_id"] == reversal["entry_id"]
    # Both entries stay on the file: nothing is edited away.
    assert len(a.journal_entries(conn)) == 2


def test_an_entry_is_reversed_only_once_and_a_reversal_can_be_reversed():
    conn, _ = with_chart("reverse_once")
    original = entry(conn, "e1", ("5000", "debit", 100), ("1000", "credit", 100))
    first = a.reverse_journal_entry(conn, original["entry_id"], "r1")
    rejected(lambda: a.reverse_journal_entry(conn, original["entry_id"], "r2"),
             f"was already reversed by entry {first['entry_id']}", a.InvalidState)
    second = a.reverse_journal_entry(conn, first["entry_id"], "r3", entry_date="2026-04-01",
                                     memo="Undoing the reversal")
    assert second["reverses_id"] == first["entry_id"] and second["entry_date"] == "2026-04-01"
    assert second["memo"] == "Undoing the reversal"
    assert a.account_by_code(conn, "5000")["balance_cents"] == 100
    rejected(lambda: a.reverse_journal_entry(conn, 99, "r4"), "no journal entry 99", a.NotFound)


def test_an_entry_on_a_closed_account_is_not_reversed_until_it_is_reopened():
    conn, _ = with_chart("reverse_closed")
    posted = entry(conn, "e1", ("1010", "debit", 100), ("3000", "credit", 100))
    petty = a.account_by_code(conn, "1010")["account_id"]
    a.update_account(conn, petty, {"is_active": False}, "close")
    rejected(lambda: a.reverse_journal_entry(conn, posted["entry_id"], "r1"),
             "posts to closed account(s) 1010", a.InvalidState)
    a.update_account(conn, petty, {"is_active": True}, "reopen")
    assert a.reverse_journal_entry(conn, posted["entry_id"], "r1")["reverses_id"] == posted["entry_id"]


def test_raw_sql_can_t_edit_delete_or_unbalance_the_journal():
    conn, _ = with_chart("raw_journal")
    posted = entry(conn, "e1", ("1000", "debit", 100), ("3000", "credit", 100))
    eid = posted["entry_id"]
    rejected(lambda: conn.execute("UPDATE acct_journal_entries SET total_cents = 1 WHERE id = ?", (eid,)),
             "journal entries are immutable")
    rejected(lambda: conn.execute("DELETE FROM acct_journal_entries WHERE id = ?", (eid,)),
             "journal entries are immutable")
    rejected(lambda: conn.execute("UPDATE acct_journal_lines SET debit_cents = 1 WHERE entry_id = ?", (eid,)),
             "journal lines are immutable")
    rejected(lambda: conn.execute("DELETE FROM acct_journal_lines WHERE entry_id = ?", (eid,)),
             "journal lines are immutable")
    # Nothing is added to a finished entry.
    rejected(lambda: conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents)
                                     VALUES (?, 3, 1, 50)""", (eid,)),
             "written before the entry itself, and never added to after")


def test_raw_sql_can_t_write_an_unbalanced_or_one_sided_entry():
    conn, _ = with_chart("raw_balance")
    bank = a.account_by_code(conn, "1000")["account_id"]
    capital = a.account_by_code(conn, "3000")["account_id"]

    def raw_entry(lines, total, entry_id=99, key="raw"):
        """Lines first, then the entry row that closes them: the way accounting.py writes one."""
        conn.execute("BEGIN IMMEDIATE")
        try:
            for line_no, (account, debit, credit) in enumerate(lines, start=1):
                conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents,
                                                                credit_cents) VALUES (?, ?, ?, ?, ?)""",
                             (entry_id, line_no, account, debit, credit))
            conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source, total_cents,
                                                              idempotency_key)
                            VALUES (?, '2026-01-05', 'Raw', 'manual', ?, ?)""", (entry_id, total, key))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    rejected(lambda: raw_entry([(bank, 100, 0), (capital, 0, 99)], 100),
             "debits must equal credits, and both must equal the entry total")
    rejected(lambda: raw_entry([(bank, 100, 0), (capital, 0, 100)], 99),
             "debits must equal credits, and both must equal the entry total")
    rejected(lambda: raw_entry([(bank, 100, 0)], 100), "a journal entry needs at least two lines")
    rejected(lambda: raw_entry([(bank, 100, 100), (capital, 0, 100)], 100), "CHECK constraint failed")
    rejected(lambda: raw_entry([(bank, 0, 0), (capital, 0, 100)], 100), "CHECK constraint failed")
    assert a.journal_entries(conn) == []


def test_raw_sql_can_t_write_a_reversal_that_doesn_t_mirror():
    conn, _ = with_chart("raw_reversal")
    posted = entry(conn, "e1", ("1000", "debit", 100), ("3000", "credit", 100))
    bank = a.account_by_code(conn, "1000")["account_id"]
    capital = a.account_by_code(conn, "3000")["account_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        # The same amounts on the same accounts, but not swapped: the two entries would double the
        # balance instead of cancelling it.
        for line_no, (account, debit, credit) in enumerate([(bank, 100, 0), (capital, 0, 100)], start=1):
            conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents,
                                                            credit_cents) VALUES (2, ?, ?, ?, ?)""",
                         (line_no, account, debit, credit))
        rejected(lambda: conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source,
                                                 total_cents, reverses_id, idempotency_key)
                                         VALUES (2, '2026-01-06', 'Fake reversal', 'manual', 100, ?, 'raw')""",
                                      (posted["entry_id"],)),
                 "a reversal must mirror the entry it reverses, line for line")
    finally:
        conn.execute("ROLLBACK")
    assert len(a.journal_entries(conn)) == 1


def test_raw_sql_can_t_post_to_a_closed_account_or_an_unknown_source():
    conn, _ = with_chart("raw_misc")
    petty = a.account_by_code(conn, "1010")["account_id"]
    a.update_account(conn, petty, {"is_active": False}, "close")
    rejected(lambda: conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents)
                                     VALUES (50, 1, ?, 100)""", (petty,)),
             "a journal line posts to an active account")
    bank = a.account_by_code(conn, "1000")["account_id"]
    capital = a.account_by_code(conn, "3000")["account_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Two good lines, so the entry row gets as far as its source: only the names in
        # acct_entry_sources are allowed, and a made-up one is refused by the foreign key.
        conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents)
                        VALUES (50, 1, ?, 100)""", (bank,))
        conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, credit_cents)
                        VALUES (50, 2, ?, 100)""", (capital,))
        rejected(lambda: conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source,
                                                 total_cents, idempotency_key)
                                         VALUES (50, '2026-01-05', 'Odd', 'guesswork', 100, 'raw')"""),
                 "FOREIGN KEY constraint failed")
    finally:
        conn.execute("ROLLBACK")
    assert a.journal_entries(conn) == []


# ---------------------------------------------------------------- idempotency

def test_replaying_a_key_returns_the_first_answer_and_writes_nothing():
    conn, _ = with_chart("replay")
    first = entry(conn, "once", ("1000", "debit", 100), ("3000", "credit", 100))
    after_first = dump(conn)
    again = entry(conn, "once", ("1000", "debit", 100), ("3000", "credit", 100))
    assert again == first and dump(conn) == after_first
    assert len(a.journal_entries(conn)) == 1


def test_the_same_key_for_different_work_is_a_conflict():
    conn, _ = with_chart("conflict")
    entry(conn, "key", ("1000", "debit", 100), ("3000", "credit", 100))
    rejected(lambda: entry(conn, "key", ("1000", "debit", 200), ("3000", "credit", 200)),
             "was already used for post_journal_entry", a.IdempotencyConflict)
    rejected(lambda: a.add_account(conn, "key", "9000", "Other", "expense"),
             "was already used for post_journal_entry", a.IdempotencyConflict)
    # A key a command stamped onto a row it wrote (here the entry itself) can't be reused either.
    rejected(lambda: a.add_account(conn, "chart:1000", "9001", "Other", "expense"),
             "was already used", a.IdempotencyConflict)


def test_an_idempotency_key_is_text_within_the_cap():
    conn, _ = with_chart("keys")
    for key in ("", "   ", None, 7, "k" * (a.MAX_TEXT + 1)):
        rejected(lambda k=key: entry(conn, k, ("1000", "debit", 100), ("3000", "credit", 100)),
                 "idempotency_key must be", ValueError)
    assert entry(conn, "k" * a.MAX_TEXT, ("1000", "debit", 100), ("3000", "credit", 100))["entry_id"] == 1


def test_eight_threads_posting_at_once_each_write_one_entry():
    """BEGIN IMMEDIATE serialises the writers, and ids are handed out under that lock, so no two entries
    share an id and none is lost."""
    conn, path = with_chart("race")
    conn.close()
    errors, barrier = [], threading.Barrier(8)

    def worker(n):
        own = a.connect(path)
        try:
            barrier.wait()
            entry(own, f"race:{n}", ("1000", "debit", 100), ("4000", "credit", 100))
        except Exception as e:                                                   # pragma: no cover
            errors.append(e)
        finally:
            own.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    conn = a.connect(path)
    assert [e["entry_id"] for e in a.journal_entries(conn)] == list(range(1, 9))
    assert a.account_by_code(conn, "1000")["balance_cents"] == 800
    assert a.trial_balance(conn, as_of="2026-12-31")["balanced"]


# ---------------------------------------------------------------- input limits

def test_text_is_normalised_and_capped():
    conn, _ = new_db("text")
    account = a.add_account(conn, "k1", "1000", "  Caf\u00e9   Bank ", "asset",
                            notes="x" * a.MAX_LONG_TEXT)
    assert account["name"] == "Caf\u00e9 Bank"                                   # NFC, trimmed, collapsed
    rejected(lambda: a.add_account(conn, "k2", "n" * (a.MAX_TEXT + 1), "Long", "asset"),
             f"code must be at most {a.MAX_TEXT} characters", ValueError)
    rejected(lambda: a.add_account(conn, "k3", "1001", "Long", "asset", notes="x" * (a.MAX_LONG_TEXT + 1)),
             f"notes must be at most {a.MAX_LONG_TEXT} characters", ValueError)
    rejected(lambda: a.add_account(conn, "k4", "   ", "Blank", "asset"), "must be a non-empty string", ValueError)
    rejected(lambda: a.add_account(conn, "k5", "1002", 7, "asset"), "must be a non-empty string", ValueError)


def test_an_entry_can_t_be_made_enormous():
    conn, _ = with_chart("big")
    lines = [{"account_code": "1000", "debit_cents": 1} for _ in range(a.MAX_LINES + 1)]
    rejected(lambda: a.post_journal_entry(conn, "huge", "2026-01-05", "Too many", lines),
             f"at most {a.MAX_LINES} lines", ValueError)
    # One line inside the cap, but a total over it, so the whole entry is refused.
    rejected(lambda: a.post_journal_entry(conn, "over", "2026-01-05", "Too much",
                                          [{"account_code": "1000", "debit_cents": a.MAX_CENTS},
                                           {"account_code": "1010", "debit_cents": 1},
                                           {"account_code": "3000", "credit_cents": a.MAX_CENTS},
                                           {"account_code": "3000", "credit_cents": 1}]),
             f"must be at most {a.MAX_CENTS} cents", ValueError)


def test_an_id_no_row_can_have_is_not_found_and_a_non_integer_is_a_value_error():
    conn, _ = with_chart("ids")
    for bad in (0, -1, 2 ** 63):
        rejected(lambda v=bad: a.account(conn, v), "no account", a.NotFound)
        rejected(lambda v=bad: a.journal_entry(conn, v), "no journal entry", a.NotFound)
    for bad in ("1", 1.0, True, None):
        rejected(lambda v=bad: a.account(conn, v), "id must be an integer", ValueError)
    rejected(lambda: a.update_account(conn, 0, {"name": "X"}, "k"), "must be a positive integer", ValueError)


def test_dates_must_be_real_days():
    conn, _ = with_chart("dates")
    for bad in ("2026-02-30", "2026-04-31", "2025-02-29", "2026-13-01", "2026-1-05", "05/01/2026", "", None):
        rejected(lambda v=bad: entry(conn, f"d:{v}", ("1000", "debit", 100), ("3000", "credit", 100), date=v),
                 "must be a date as 'YYYY-MM-DD'", ValueError)
    assert entry(conn, "leap", ("1000", "debit", 100), ("3000", "credit", 100),
                 date="2024-02-29")["entry_date"] == "2024-02-29"


def test_deletes_are_refused_even_on_a_connection_with_foreign_keys_off():
    """The triggers don't depend on PRAGMA foreign_keys, so a client that turns it off still can't
    empty the journal."""
    conn, path = with_chart("fk_off")
    entry(conn, "e1", ("1000", "debit", 100), ("3000", "credit", 100))
    bare = sqlite3.connect(path, isolation_level=None)
    bare.execute("PRAGMA foreign_keys = OFF")
    try:
        rejected(lambda: bare.execute("DELETE FROM acct_journal_entries"), "journal entries are immutable")
        rejected(lambda: bare.execute("DELETE FROM acct_journal_lines"), "journal lines are immutable")
        rejected(lambda: bare.execute("DELETE FROM acct_accounts"), "accounts are kept")
        rejected(lambda: bare.execute("DELETE FROM acct_commands"), "acct_commands rows are immutable")
        rejected(lambda: bare.execute("UPDATE acct_commands SET result = '{}'"),
                 "acct_commands rows are immutable")
    finally:
        bare.close()


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}")
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
