"""
Accounting module, v1: a chart of accounts, a double-entry journal, customers
and suppliers, invoices and bills, the payments that settle them, and the five
reports a small business is asked for. Standalone: stdlib sqlite3 only, nothing
from operations_nerd/, no LLM calls. Built in the same way as the HR module
(modules/hr/ on daksh/hr-module) and the logistics stock engine
(modules/logistics/ on jasmeet/logistics-stock).

Each business has its own private database, so nothing here takes a business
id: a connection is one business's books. Every table is prefixed `acct_`.

The journal is the only place a figure lives. acct_journal_entries and
acct_journal_lines are append-only: an entry is written once, with its lines,
and never changes. Debits equal credits for every entry, checked by the
database itself (migration 0001), so no code path can leave the books
unbalanced. A mistake is corrected by a reversing entry, never by an edit.

Invoices and bills are documents, not balances. What an invoice does to the
books is the journal entry it points at; its own row carries who, when, how
much and what state it is in, so a person can read it and so the aging reports
know when the money is due. Sending an invoice debits receivables and credits
income; receiving a bill debits expenses and credits payables; a payment moves
the amount between the control account and the money account. Every figure the
reports show is read back out of the journal.

Money is whole cents in INTEGER columns, everywhere, in and out: no amount is
ever a float, so no sum ever drifts. Amounts are capped at MAX_CENTS, far above
any small business's numbers, so every sum stays inside SQLite's 64-bit
integers.

Statuses are draft, sent, paid and void. 'overdue' is not one of them: whether
a document is overdue depends on what day it is, so storing it would mean a row
that goes stale on its own. Queries derive it from the due date and an `as_of`
date instead, and return it as `effective_status`.

Every command runs in one BEGIN IMMEDIATE transaction and takes an idempotency
key that is unique across all commands. Replaying a key with the same command
and payload returns the original result and writes nothing; the same key with a
different command or payload raises IdempotencyConflict. A command that fails
records nothing, so its key stays free.

Open connections with connect(); it sets autocommit mode, which the explicit
BEGIN IMMEDIATE relies on, and turns on foreign keys. init_db() brings the
schema up to date through the versioned migrations in migrations/ (see
migrate.py), after backing up a database that already has tables.

Dates are 'YYYY-MM-DD' and come from the caller on every command. Read-only
queries and reports take an `as_of` (or a date range) and fall back to the
machine's date only when it isn't given; nothing stores it.

Text. Names, codes, numbers and descriptions are stored NFC-normalised with
surrounding whitespace trimmed and inner runs of whitespace collapsed to one
space; email addresses are also lowercased. Every text input has a length cap
(MAX_TEXT, or MAX_LONG_TEXT for notes, terms and memos).

Trust boundary. The triggers are the rules for anyone who writes through
SQLite, but whoever can write the database file can also drop them. They are
not a defence against that person. verify_schema() compares the triggers and
indexes with what the migrations create and raises SchemaTampered on any
difference. init_db() runs it, and the API runs it on every request, so a
tampered schema stops the module (fails closed) instead of running without its
rules. It can't detect rows that were changed while a rule was missing.

Payroll. post_payroll_entry() is the documented, unwired hook for the HR
module: it posts an entry with source 'payroll'. Nothing in this module or in
HR calls it yet; see README, "Hook for HR payroll".
"""

import datetime
import functools
import importlib.util
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path


def _load_migrate():
    """The versioned-migration helper, loaded from this folder under a name of its own.

    modules/hr/migrate.py is the same file under the same module name, and both modules put
    their own folder on sys.path, so a plain `import migrate` would hand whichever copy loaded
    first to the other module. Loading it by path keeps the two apart until one shared copy
    replaces both (README, "Notes for Amaan and the team").
    """
    name = "accounting_migrate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name("migrate.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_migrate = _load_migrate()

MODULE = "accounting"
MIGRATIONS = Path(__file__).resolve().with_name("migrations")
BUSY_TIMEOUT_SECONDS = 10
# The largest id SQLite can store; a bigger number can't name a row (and sqlite3 raises OverflowError on it).
MAX_ID = 2 ** 63 - 1
# A cap on any one amount, in cents, matching the CHECKs in the migrations: 100 billion in whole currency.
# Far above any small business's numbers, so amounts and their sums always fit SQLite's 64-bit integers.
MAX_CENTS = 10_000_000_000_000
# Length caps, in characters: names, codes, numbers and descriptions; and notes, terms and memos.
MAX_TEXT = 200
MAX_LONG_TEXT = 2000
# A cap on how many lines one entry or document can have, so one call can't be made huge.
MAX_LINES = 500
MAX_QUANTITY = 1_000_000

ACCOUNT_TYPES = ("asset", "liability", "equity", "income", "expense")
# The two accounts a posting rule needs to find by meaning rather than by number.
ACCOUNT_ROLES = ("accounts_receivable", "accounts_payable")
# Types whose natural balance is a debit; the rest are natural credits.
DEBIT_TYPES = ("asset", "expense")
# What post_journal_entry may write. The others ('invoice', 'bill', 'invoice_payment',
# 'bill_payment') are written only by the document commands, so a hand-posted entry can
# never be mistaken for one of theirs.
MANUAL_SOURCES = ("manual", "payroll")
DOCUMENT_STATUSES = ("draft", "sent", "paid", "void")
# Fields add_customer / add_supplier take, and update_customer / update_supplier may change.
PARTY_FIELDS = ("name", "email", "phone", "billing_address", "payment_terms_days", "terms", "notes")
UPDATABLE_PARTY_FIELDS = PARTY_FIELDS + ("is_active",)
_AGING_BUCKETS = ("current", "1_30", "31_60", "61_90", "over_90")


class AccountingError(Exception):
    pass


class IdempotencyConflict(AccountingError):
    """The key was already used for a different command or payload."""


class NotFound(AccountingError):
    """No such account, customer, supplier, invoice, bill or journal entry."""


class InvalidState(AccountingError):
    """The account or document is not in a state for this command (a draft invoice can't be paid,
    a paid one can't be voided, a code is already taken...)."""


class Unbalanced(AccountingError):
    """The lines of a journal entry don't have debits equal to credits."""


class SchemaTampered(AccountingError):
    """The database's accounting triggers or indexes aren't the ones the migrations create."""


def connect(path: str, check_same_thread: bool = True, create: bool = True) -> sqlite3.Connection:
    """check_same_thread=False is for the API, where FastAPI may open and use one request's connection on two
    worker threads; the connection is still used by one request at a time. create=False opens only a database
    file that exists (sqlite3.OperationalError otherwise) instead of creating an empty one."""
    if create or path == ":memory:":
        conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_SECONDS,
                               check_same_thread=check_same_thread)
    else:
        conn = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=rw", uri=True, isolation_level=None,
                               timeout=BUSY_TIMEOUT_SECONDS, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn: sqlite3.Connection, backup_dir: str | None = None) -> dict:
    """Applies any missing accounting migrations, after backing up a database that already has tables, then
    checks the schema with verify_schema() (SchemaTampered if it was changed outside the migrations)."""
    result = _migrate.migrate(conn, MODULE, MIGRATIONS, backup_dir=backup_dir)
    verify_schema(conn)
    return result


def schema_version(conn) -> int:
    return _migrate.current_version(conn, MODULE)


def latest_version() -> int:
    """The version the migrations in this code bring a database to."""
    return len(_migrate.migrations(MIGRATIONS))


def verify_schema(conn) -> None:
    """Raises SchemaTampered unless the database's accounting triggers and indexes are exactly the ones the
    migrations up to its version create (same names, same SQL up to whitespace), and its acct_ tables are the
    ones they create. Changed or dropped rules, and extra acct_ objects, all count."""
    version = schema_version(conn)
    expected = _expected_schema(version)
    actual = {r["name"]: (r["type"], _normalise_sql(r["sql"]))
              for r in conn.execute("""SELECT type, name, sql FROM sqlite_master
                                        WHERE type IN ('table', 'index', 'trigger', 'view')
                                          AND name NOT LIKE 'sqlite_%'
                                          AND (name LIKE 'acct\\_%' ESCAPE '\\'
                                               OR tbl_name LIKE 'acct\\_%' ESCAPE '\\')""")}
    problems = []
    for name, (kind, sql) in sorted(expected.items()):
        if name not in actual:
            problems.append(f"{kind} {name} is missing")
        elif actual[name][0] != kind:
            problems.append(f"{name} is a {actual[name][0]}, not a {kind}")
        elif kind != "table" and actual[name][1] != sql:
            problems.append(f"{kind} {name} was changed")
    problems += [f"unexpected {kind} {name}" for name, (kind, _) in sorted(actual.items()) if name not in expected]
    if problems:
        raise SchemaTampered(f"the accounting schema (version {version}) isn't what its migrations create: "
                             + "; ".join(problems))


@functools.lru_cache(maxsize=None)
def _expected_schema(version: int) -> dict:
    """{name: (type, normalised sql)} of the acct_ objects migrations 1..version leave behind. Tables are
    compared by name only: SQLite rewrites a table's SQL on ALTER TABLE."""
    out = {}
    for _, _, path in _migrate.migrations(MIGRATIONS)[:version]:
        for sql in _migrate._statements(path.read_text()):
            text = _normalise_sql(sql)
            m = re.match(r"CREATE (?:UNIQUE )?(TABLE|INDEX|TRIGGER|VIEW) (?:IF NOT EXISTS )?(\w+)", text, re.I)
            if m:
                out[m.group(2)] = (m.group(1).lower(), text)
                continue
            m = re.match(r"DROP (TABLE|INDEX|TRIGGER|VIEW) (?:IF EXISTS )?(\w+)", text, re.I)
            if m:
                out.pop(m.group(2), None)
                if m.group(1).upper() == "TABLE":     # its indexes and triggers go with it
                    out = {k: v for k, v in out.items()
                           if not (v[0] in ("index", "trigger")
                                   and re.search(rf"\bON {re.escape(m.group(2))}\b", v[1], re.I))}
                continue
            m = re.match(r"ALTER TABLE (\w+) RENAME TO (\w+)", text, re.I)
            if m:
                out[m.group(2)] = out.pop(m.group(1))
    return out


def _normalise_sql(sql) -> str:
    """The statement as sqlite_master keeps it: from CREATE on, no trailing ';', whitespace collapsed."""
    lines = (sql or "").strip().splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return " ".join("\n".join(lines).strip().rstrip(";").split())


# ---------------------------------------------------------------- chart of accounts

def add_account(conn, idempotency_key: str, code: str, name: str, account_type: str, role: str | None = None,
                notes: str | None = None) -> dict:
    """Adds an account to the chart. Returns account().

    code is the business's own number ('1100'), unique, and never changes; so do account_type and role.
    role marks the one account a posting rule needs by meaning rather than by number:
    'accounts_receivable' (an asset) or 'accounts_payable' (a liability). At most one account holds
    each role, and invoices and bills need them.
    """
    code = _clean_text("code", code)
    name = _clean_text("name", name)
    if account_type not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of {', '.join(ACCOUNT_TYPES)}, got {account_type!r}")
    if role is not None and role not in ACCOUNT_ROLES:
        raise ValueError(f"role must be one of {', '.join(ACCOUNT_ROLES)} or None, got {role!r}")
    if role == "accounts_receivable" and account_type != "asset":
        raise ValueError("the accounts_receivable account is an asset: what customers owe is money coming in")
    if role == "accounts_payable" and account_type != "liability":
        raise ValueError("the accounts_payable account is a liability: what the business owes suppliers")
    if notes is not None:
        notes = _clean_text("notes", notes, MAX_LONG_TEXT)

    def work():
        taken = conn.execute("SELECT id FROM acct_accounts WHERE code = ?", (code,)).fetchone()
        if taken:
            raise InvalidState(f"account code {code!r} is already account {taken['id']}")
        taken = conn.execute("SELECT id FROM acct_accounts WHERE lower(name) = lower(?)", (name,)).fetchone()
        if taken:
            raise InvalidState(f"account name {name!r} is already account {taken['id']}'s")
        if role is not None:
            taken = conn.execute("SELECT id, code FROM acct_accounts WHERE role = ?", (role,)).fetchone()
            if taken:
                raise InvalidState(f"account {taken['code']} is already the {role} account")
        account_id = conn.execute("""INSERT INTO acct_accounts (code, name, account_type, role, notes,
                                                                idempotency_key)
                                     VALUES (?, ?, ?, ?, ?, ?)""",
                                  (code, name, account_type, role, notes, idempotency_key)).lastrowid
        return account(conn, account_id)

    payload = {"code": code, "name": name, "account_type": account_type, "role": role, "notes": notes}
    return _run(conn, idempotency_key, "add_account", payload, work)


def update_account(conn, account_id: int, changes: dict, idempotency_key: str) -> dict:
    """Changes an account's name, notes or is_active. Returns account().

    An account's code, type and role never change: the journal behind a code has to keep meaning the
    same thing. Deactivating one keeps its history and its balance but refuses new postings to it;
    an account is never deleted.
    """
    _positive_int("account_id", account_id)
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty dict of fields")
    allowed = ("name", "notes", "is_active")
    unknown = set(changes) - set(allowed)
    if unknown:
        raise ValueError(f"these fields can't be updated: {', '.join(sorted(unknown))}; "
                         f"an account's code, type and role never change")
    clean = {}
    if "name" in changes:
        clean["name"] = _clean_text("name", changes["name"])
    if "notes" in changes:
        clean["notes"] = None if changes["notes"] is None else _clean_text("notes", changes["notes"], MAX_LONG_TEXT)
    if "is_active" in changes:
        _bool("is_active", changes["is_active"])
        clean["is_active"] = int(changes["is_active"])

    def work():
        current = _account_row(conn, account_id)
        if "name" in clean:
            taken = conn.execute("SELECT id FROM acct_accounts WHERE lower(name) = lower(?) AND id <> ?",
                                 (clean["name"], account_id)).fetchone()
            if taken:
                raise InvalidState(f"account name {clean['name']!r} is already account {taken['id']}'s")
        if not any(current[k] != v for k, v in clean.items()):
            raise ValueError(f"nothing changes for account {account_id}")
        if clean.get("is_active") == 0 and current["role"] is not None:
            raise InvalidState(f"account {current['code']} is the {current['role']} account; "
                               f"invoices and bills need it, so it stays active")
        sets = ", ".join(f"{k} = ?" for k in clean)
        conn.execute(f"UPDATE acct_accounts SET {sets} WHERE id = ?", list(clean.values()) + [account_id])
        return account(conn, account_id)

    payload = {"account_id": account_id, "changes": clean}
    return _run(conn, idempotency_key, "update_account", payload, work)


# ---------------------------------------------------------------- the journal

def post_journal_entry(conn, idempotency_key: str, entry_date: str, memo: str, lines: list[dict],
                       source: str = "manual", source_ref: str | None = None) -> dict:
    """Posts one balanced journal entry. Returns journal_entry().

    lines is a list of at least two dicts, each naming an account by 'account_id' or 'account_code'
    and carrying exactly one of 'debit_cents' or 'credit_cents' (a positive whole number of cents),
    and an optional 'memo'. Debits must equal credits (Unbalanced otherwise), every account must be
    active, and the entry is immutable once written: correct it with reverse_journal_entry.

    source says what the entry is for and must be 'manual' or 'payroll' (see post_payroll_entry);
    the sources invoices, bills and payments use are written only by their own commands.
    """
    _date("entry_date", entry_date)
    memo = _clean_text("memo", memo, MAX_LONG_TEXT)
    if source not in MANUAL_SOURCES:
        raise ValueError(f"source must be one of {', '.join(MANUAL_SOURCES)}, got {source!r}: "
                         f"invoices, bills and payments post their own entries")
    if source_ref is not None:
        source_ref = _clean_text("source_ref", source_ref)
    prepared = _check_lines(lines)

    def work():
        resolved = [_resolve_line(conn, line) for line in prepared]
        entry_id = _insert_entry(conn, entry_date, memo, source, source_ref, resolved, idempotency_key)
        return journal_entry(conn, entry_id)

    payload = {"entry_date": entry_date, "memo": memo, "source": source, "source_ref": source_ref,
               "lines": prepared}
    return _run(conn, idempotency_key, "post_journal_entry", payload, work)


def post_payroll_entry(conn, idempotency_key: str, entry_date: str, memo: str, lines: list[dict],
                       source_ref: str | None = None) -> dict:
    """The hook for HR payroll, documented and deliberately unwired: nothing in this module, and nothing
    in modules/hr/, calls it. Returns journal_entry().

    It is post_journal_entry() with source = 'payroll', so a pay run's entries can be told apart from
    hand-posted ones (journal_entries(source='payroll')) and found again by source_ref (the pay run's
    reference). When payroll is built, it should work out the gross, the deductions and the net itself
    and hand the finished, balanced lines here, one entry per pay run, with the pay run's own id as the
    idempotency key, so a retried pay run posts once. A payroll entry usually looks like:

        debit  wages expense        gross pay
        credit PAYE / tax payable   tax withheld
        credit pension payable      pension withheld
        credit net wages payable    what the staff are owed

    and paying the staff is a second entry (debit net wages payable, credit the bank account).
    This module deliberately holds no pay figure of its own: see README, "Hook for HR payroll", and
    modules/hr/README.md, "No payroll or sensitive fields".
    """
    return post_journal_entry(conn, idempotency_key, entry_date, memo, lines, source="payroll",
                              source_ref=source_ref)


def reverse_journal_entry(conn, entry_id: int, idempotency_key: str, entry_date: str | None = None,
                          memo: str | None = None) -> dict:
    """Posts the mirror image of an entry, so the two together leave every account where it was.
    Returns journal_entry() of the reversal.

    entry_date defaults to the original entry's date, memo to 'Reversal of entry N: <its memo>'. An
    entry is reversed at most once (InvalidState); correct a reversal by reversing the reversal.
    Only a manual or payroll entry: an invoice or a bill is reversed by voiding it, and a payment
    entry is not reversed (there is no refund in v1). Reversing one of those on its own would take
    the figure out of the journal and leave the document still saying the money is there.
    """
    _positive_int("entry_id", entry_id)
    if entry_date is not None:
        _date("entry_date", entry_date)
    if memo is not None:
        memo = _clean_text("memo", memo, MAX_LONG_TEXT)

    def work():
        reversal_id = _insert_reversal(conn, entry_id, idempotency_key, entry_date, memo)
        return journal_entry(conn, reversal_id)

    payload = {"entry_id": entry_id, "entry_date": entry_date, "memo": memo}
    return _run(conn, idempotency_key, "reverse_journal_entry", payload, work)


# ---------------------------------------------------------------- customers and suppliers

def add_customer(conn, idempotency_key: str, name: str, **fields) -> dict:
    """Adds a customer. Returns customer().

    Optional: email, phone, billing_address, payment_terms_days (how many days after its issue date an
    invoice falls due, by default), terms and notes. Names are unique, case-insensitively.
    """
    return _add_party(conn, "customer", idempotency_key, name, fields)


def update_customer(conn, customer_id: int, changes: dict, idempotency_key: str) -> dict:
    """Changes a customer's details, or is_active (which keeps their invoices but refuses new ones)."""
    return _update_party(conn, "customer", customer_id, changes, idempotency_key)


def add_supplier(conn, idempotency_key: str, name: str, **fields) -> dict:
    """Adds a supplier. Takes the same fields as add_customer(). Returns supplier()."""
    return _add_party(conn, "supplier", idempotency_key, name, fields)


def update_supplier(conn, supplier_id: int, changes: dict, idempotency_key: str) -> dict:
    """Changes a supplier's details, or is_active."""
    return _update_party(conn, "supplier", supplier_id, changes, idempotency_key)


# ---------------------------------------------------------------- invoices

def create_invoice(conn, idempotency_key: str, customer_id: int, invoice_number: str, issue_date: str,
                   lines: list[dict], due_date: str | None = None, terms: str | None = None,
                   notes: str | None = None) -> dict:
    """Raises a draft invoice. Returns invoice(). Nothing is posted to the books until send_invoice().

    lines is a list of at least one dict: 'description', 'quantity' (a whole number of units),
    'unit_price_cents', and the income account the sale belongs to ('account_id' or 'account_code').
    A line's amount is quantity * unit_price_cents, exactly, and the invoice's total is the sum of
    its lines. due_date defaults to issue_date plus the customer's payment_terms_days and can't be
    before the issue date. invoice_number is unique and, like every other field here, never changes:
    a wrong invoice is voided and raised again.

    terms and notes are free text for whatever the two sides agreed. Nothing reads them; agreements
    are out of scope for v1 (README).
    """
    _positive_int("customer_id", customer_id)
    invoice_number = _clean_text("invoice_number", invoice_number)
    _date("issue_date", issue_date)
    if due_date is not None:
        _date("due_date", due_date)
    terms = None if terms is None else _clean_text("terms", terms, MAX_LONG_TEXT)
    notes = None if notes is None else _clean_text("notes", notes, MAX_LONG_TEXT)
    prepared = _check_document_lines(lines)

    def work():
        party = _party_row(conn, "customer", customer_id)
        if not party["is_active"]:
            raise InvalidState(f"customer {customer_id} is not active")
        taken = conn.execute("SELECT id FROM acct_invoices WHERE invoice_number = ?", (invoice_number,)).fetchone()
        if taken:
            raise InvalidState(f"invoice number {invoice_number!r} is already invoice {taken['id']}")
        falls_due = due_date or _add_days(issue_date, party["payment_terms_days"])
        if falls_due < issue_date:
            raise ValueError(f"due_date {falls_due} is before the issue date {issue_date}")
        invoice_id = _insert_document(conn, "invoice", prepared, idempotency_key, party_id=customer_id,
                                      number=invoice_number, issue_date=issue_date, due_date=falls_due,
                                      terms=terms, notes=notes)
        return invoice(conn, invoice_id)

    payload = {"customer_id": customer_id, "invoice_number": invoice_number, "issue_date": issue_date,
               "due_date": due_date, "terms": terms, "notes": notes, "lines": prepared}
    return _run(conn, idempotency_key, "create_invoice", payload, work)


def send_invoice(conn, invoice_id: int, idempotency_key: str, entry_date: str | None = None) -> dict:
    """Sends a draft invoice, which is what puts it in the books. Returns invoice().

    Posts one entry dated entry_date (the issue date by default): debit the accounts_receivable
    account by the total, credit each income account on the invoice by its share. Afterwards the
    invoice is 'sent' and payments can be recorded against it.
    """
    return _post_document(conn, "invoice", invoice_id, idempotency_key, entry_date)


def record_invoice_payment(conn, invoice_id: int, amount_cents: int, payment_date: str,
                           deposit_account_id: int, idempotency_key: str, reference: str | None = None) -> dict:
    """Records money received against a sent invoice. Returns invoice().

    Posts one entry dated payment_date: debit the account the money arrived in (an active asset
    account that isn't receivables), credit accounts_receivable. payment_date can't be before the
    invoice's own entry date, so the receipt is never in the books on a day the invoice is not.
    Part payments are fine and leave the invoice 'sent' with a smaller outstanding amount; the
    payment that clears the last cent moves it to 'paid'. More than the outstanding amount is
    refused (InvalidState): there is no credit note or refund in v1.
    """
    return _record_payment(conn, "invoice", invoice_id, amount_cents, payment_date, deposit_account_id,
                           idempotency_key, reference)


def void_invoice(conn, invoice_id: int, reason: str, idempotency_key: str, entry_date: str | None = None) -> dict:
    """Voids a draft invoice, or a sent one that nothing has been paid against. Returns invoice().

    A sent invoice's entry is reversed (dated entry_date, the original entry's date by default), so the
    books end up where they were. entry_date can't be before that entry's date: a void that landed
    first would credit receivables on a day the invoice had not been posted. An invoice with a
    payment against it is not voided (InvalidState): that would hide money that really arrived.
    Nothing is deleted and the void is not undone.
    """
    return _void_document(conn, "invoice", invoice_id, reason, idempotency_key, entry_date)


# ---------------------------------------------------------------- bills

def create_bill(conn, idempotency_key: str, supplier_id: int, bill_number: str, issue_date: str,
                lines: list[dict], due_date: str | None = None, terms: str | None = None,
                notes: str | None = None) -> dict:
    """Enters a draft bill from a supplier. Returns bill(). Nothing is posted until receive_bill().

    The mirror of create_invoice(), with two differences: a line's account is an expense account (or an
    asset account, for stock or equipment the business keeps), and bill_number is the supplier's own
    number, so it only has to be unique for that supplier.
    """
    _positive_int("supplier_id", supplier_id)
    bill_number = _clean_text("bill_number", bill_number)
    _date("issue_date", issue_date)
    if due_date is not None:
        _date("due_date", due_date)
    terms = None if terms is None else _clean_text("terms", terms, MAX_LONG_TEXT)
    notes = None if notes is None else _clean_text("notes", notes, MAX_LONG_TEXT)
    prepared = _check_document_lines(lines)

    def work():
        party = _party_row(conn, "supplier", supplier_id)
        if not party["is_active"]:
            raise InvalidState(f"supplier {supplier_id} is not active")
        taken = conn.execute("SELECT id FROM acct_bills WHERE supplier_id = ? AND bill_number = ?",
                             (supplier_id, bill_number)).fetchone()
        if taken:
            raise InvalidState(f"bill number {bill_number!r} from supplier {supplier_id} is already "
                               f"bill {taken['id']}")
        falls_due = due_date or _add_days(issue_date, party["payment_terms_days"])
        if falls_due < issue_date:
            raise ValueError(f"due_date {falls_due} is before the issue date {issue_date}")
        bill_id = _insert_document(conn, "bill", prepared, idempotency_key, party_id=supplier_id,
                                   number=bill_number, issue_date=issue_date, due_date=falls_due,
                                   terms=terms, notes=notes)
        return bill(conn, bill_id)

    payload = {"supplier_id": supplier_id, "bill_number": bill_number, "issue_date": issue_date,
               "due_date": due_date, "terms": terms, "notes": notes, "lines": prepared}
    return _run(conn, idempotency_key, "create_bill", payload, work)


def receive_bill(conn, bill_id: int, idempotency_key: str, entry_date: str | None = None) -> dict:
    """Accepts a draft bill, which is what puts it in the books. Returns bill().

    Posts one entry dated entry_date (the issue date by default): debit each expense or asset account
    on the bill by its share, credit the accounts_payable account by the total. Afterwards the bill's
    status is 'sent' -- the same four statuses are used for both sides, and for a bill 'sent' reads as
    "received and in the books, waiting to be paid" (README, "Statuses").
    """
    return _post_document(conn, "bill", bill_id, idempotency_key, entry_date)


def record_bill_payment(conn, bill_id: int, amount_cents: int, payment_date: str, paid_from_account_id: int,
                        idempotency_key: str, reference: str | None = None) -> dict:
    """Records money paid against a received bill. Returns bill().

    Posts one entry dated payment_date: debit accounts_payable, credit the account the money left from
    (an active asset account that isn't receivables). payment_date can't be before the bill's own
    entry date. Part payments are fine; more than the outstanding amount is refused.
    """
    return _record_payment(conn, "bill", bill_id, amount_cents, payment_date, paid_from_account_id,
                           idempotency_key, reference)


def void_bill(conn, bill_id: int, reason: str, idempotency_key: str, entry_date: str | None = None) -> dict:
    """Voids a draft bill, or a received one nothing has been paid against. The mirror of void_invoice()."""
    return _void_document(conn, "bill", bill_id, reason, idempotency_key, entry_date)


# ---------------------------------------------------------------- queries

def account(conn, account_id: int) -> dict:
    """One account, with its balance over the whole journal."""
    _lookup_id("account", account_id)
    row = conn.execute("SELECT * FROM acct_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound(f"no account {account_id}")
    return _account_dict(conn, row)


def account_by_code(conn, code: str) -> dict:
    code = _clean_text("code", code)
    row = conn.execute("SELECT * FROM acct_accounts WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFound(f"no account with code {code!r}")
    return _account_dict(conn, row)


def accounts(conn, account_type: str | None = None, active_only: bool = False) -> list[dict]:
    """The chart of accounts, ordered by code."""
    if account_type is not None and account_type not in ACCOUNT_TYPES:
        raise ValueError(f"account_type must be one of {', '.join(ACCOUNT_TYPES)}, got {account_type!r}")
    _bool("active_only", active_only)
    sql, args = "SELECT * FROM acct_accounts WHERE 1 = 1", []
    if account_type is not None:
        sql, args = sql + " AND account_type = ?", args + [account_type]
    if active_only:
        sql += " AND is_active = 1"
    rows = conn.execute(sql + " ORDER BY code", args).fetchall()
    balances = _balances(conn, None)                 # one query for the whole chart, not one per account
    return [_account_dict(conn, r, balances) for r in rows]


def account_balance(conn, account_id: int, as_of: str | None = None) -> dict:
    """What one account's journal lines add up to, up to and including as_of (everything, by default).

    debit_cents and credit_cents are the two totals; balance_cents is the balance on the account's
    natural side, so it is positive for an asset or expense account with a debit balance and for a
    liability, equity or income account with a credit balance.
    """
    row = _account_row(conn, account_id)
    if as_of is not None:
        _date("as_of", as_of)
    return _balances(conn, as_of).get(account_id) or _empty_balance(row)


def journal_entry(conn, entry_id: int) -> dict:
    """One entry with its lines, in line order."""
    _lookup_id("journal entry", entry_id)
    row = conn.execute("SELECT * FROM acct_journal_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise NotFound(f"no journal entry {entry_id}")
    lines = conn.execute("""SELECT l.line_no, l.account_id, a.code AS account_code, a.name AS account_name,
                                   l.debit_cents, l.credit_cents, l.memo
                            FROM acct_journal_lines l JOIN acct_accounts a ON a.id = l.account_id
                            WHERE l.entry_id = ? ORDER BY l.line_no""", (entry_id,))
    reversed_by = conn.execute("SELECT id FROM acct_journal_entries WHERE reverses_id = ?", (entry_id,)).fetchone()
    return {"entry_id": row["id"], "entry_date": row["entry_date"], "memo": row["memo"], "source": row["source"],
            "source_ref": row["source_ref"], "total_cents": row["total_cents"], "reverses_id": row["reverses_id"],
            "reversed_by_id": reversed_by["id"] if reversed_by else None,
            "lines": [dict(line) for line in lines]}


def journal_entries(conn, from_date: str | None = None, to_date: str | None = None, source: str | None = None,
                    limit: int = 500) -> list[dict]:
    """Entries by date then id, oldest first, each with its lines. from_date and to_date are inclusive."""
    for name, value in (("from_date", from_date), ("to_date", to_date)):
        if value is not None:
            _date(name, value)
    if source is not None:
        _text("source", source)
    if type(limit) is not int or not 0 < limit <= 10_000:
        raise ValueError(f"limit must be a positive integer up to 10000, got {limit!r}")
    sql, args = "SELECT id FROM acct_journal_entries WHERE 1 = 1", []
    if from_date is not None:
        sql, args = sql + " AND entry_date >= ?", args + [from_date]
    if to_date is not None:
        sql, args = sql + " AND entry_date <= ?", args + [to_date]
    if source is not None:
        sql, args = sql + " AND source = ?", args + [source]
    rows = conn.execute(sql + " ORDER BY entry_date, id LIMIT ?", args + [limit]).fetchall()
    return [journal_entry(conn, r["id"]) for r in rows]


def customer(conn, customer_id: int) -> dict:
    return _party_dict(_party_row(conn, "customer", customer_id))


def customers(conn, active_only: bool = False) -> list[dict]:
    return _parties(conn, "customer", active_only)


def supplier(conn, supplier_id: int) -> dict:
    return _party_dict(_party_row(conn, "supplier", supplier_id))


def suppliers(conn, active_only: bool = False) -> list[dict]:
    return _parties(conn, "supplier", active_only)


def invoice(conn, invoice_id: int, as_of: str | None = None) -> dict:
    """One invoice with its lines and payments.

    status is what is stored (draft, sent, paid or void); effective_status is what a person wants to
    see, which adds 'overdue' when a sent invoice's due date has passed on as_of (today by default).
    """
    return _document(conn, "invoice", invoice_id, as_of)


def invoices(conn, customer_id: int | None = None, status: str | None = None, as_of: str | None = None,
             open_only: bool = False) -> list[dict]:
    """Invoices by due date then id. status filters on the stored status, or is 'overdue' to ask for
    sent invoices past their due date on as_of. open_only keeps the ones with money outstanding."""
    return _documents(conn, "invoice", customer_id, status, as_of, open_only)


def bill(conn, bill_id: int, as_of: str | None = None) -> dict:
    """One bill with its lines and payments; see invoice() for status and effective_status."""
    return _document(conn, "bill", bill_id, as_of)


def bills(conn, supplier_id: int | None = None, status: str | None = None, as_of: str | None = None,
          open_only: bool = False) -> list[dict]:
    return _documents(conn, "bill", supplier_id, status, as_of, open_only)


# ---------------------------------------------------------------- reports

def trial_balance(conn, as_of: str | None = None, include_empty: bool = False) -> dict:
    """Every account's debit and credit totals up to as_of (today by default), and the two grand totals.

    The grand totals are equal in a double-entry ledger, and the database makes every single entry
    balance, so 'balanced' is there to be checked rather than hoped for: if it is ever false, someone
    has written to the file around the module's rules.
    """
    as_of = _as_of(as_of)
    _bool("include_empty", include_empty)
    balances = _balances(conn, as_of)
    rows = []
    for row in conn.execute("SELECT * FROM acct_accounts ORDER BY code"):
        line = balances.get(row["id"]) or _empty_balance(row)
        if include_empty or line["debit_cents"] or line["credit_cents"]:
            rows.append(line)
    debits = sum(r["debit_cents"] for r in rows)
    credits = sum(r["credit_cents"] for r in rows)
    return {"as_of": as_of, "rows": rows, "total_debit_cents": debits, "total_credit_cents": credits,
            "balanced": debits == credits}


def profit_and_loss(conn, from_date: str, to_date: str) -> dict:
    """Income less expenses over a date range, both dates inclusive, by account.

    Each amount is on the account's natural side: income is credits less debits (so a refund posted
    against an income account reduces it), expenses are debits less credits.
    """
    _date("from_date", from_date)
    _date("to_date", to_date)
    if to_date < from_date:
        raise ValueError(f"to_date {to_date} is before from_date {from_date}")
    balances = _balances(conn, to_date, from_date=from_date)
    income = [b for b in balances.values() if b["account_type"] == "income"]
    expenses = [b for b in balances.values() if b["account_type"] == "expense"]
    income.sort(key=lambda b: b["code"])
    expenses.sort(key=lambda b: b["code"])
    total_income = sum(b["balance_cents"] for b in income)
    total_expense = sum(b["balance_cents"] for b in expenses)
    return {"from_date": from_date, "to_date": to_date, "income": income, "expenses": expenses,
            "total_income_cents": total_income, "total_expense_cents": total_expense,
            "net_income_cents": total_income - total_expense}


def balance_sheet(conn, as_of: str | None = None) -> dict:
    """What the business owns, owes and is worth on as_of (today by default).

    Nothing here closes the year into an equity account, so the profit the business has made since
    the books opened is reported separately as retained_earnings_cents (income less expenses up to
    as_of). Assets then equal liabilities plus equity plus retained earnings, which is what
    'balanced' checks.
    """
    as_of = _as_of(as_of)
    balances = _balances(conn, as_of)
    groups = {t: sorted((b for b in balances.values() if b["account_type"] == t), key=lambda b: b["code"])
              for t in ACCOUNT_TYPES}
    totals = {t: sum(b["balance_cents"] for b in groups[t]) for t in ACCOUNT_TYPES}
    retained = totals["income"] - totals["expense"]
    return {"as_of": as_of, "assets": groups["asset"], "liabilities": groups["liability"],
            "equity": groups["equity"], "total_assets_cents": totals["asset"],
            "total_liabilities_cents": totals["liability"], "total_equity_cents": totals["equity"],
            "retained_earnings_cents": retained,
            "balanced": totals["asset"] == totals["liability"] + totals["equity"] + retained}


def ar_aging(conn, as_of: str | None = None) -> dict:
    """What customers owed on as_of (today by default), by customer and by how late it is.

    An invoice counts when its own entry is dated on or before as_of and it had not been voided by
    then, less the payments received by then; so the total equals the accounts_receivable balance on
    as_of, unless someone has hand-posted a journal entry to that account (the reports read the
    journal, the aging reads the documents). Buckets are current (not due yet), 1_30, 31_60, 61_90
    and over_90 days past the due date.
    """
    return _aging(conn, "invoice", as_of)


def ap_aging(conn, as_of: str | None = None) -> dict:
    """What the business owed suppliers on as_of, by supplier and by how late it is. The mirror of ar_aging()."""
    return _aging(conn, "bill", as_of)


# ---------------------------------------------------------------- internals: documents

# One place for everything that differs between the two sides of the ledger, so the invoice and bill
# commands really are the same code and can't drift apart.
_KINDS = {
    "invoice": {
        "table": "acct_invoices", "lines": "acct_invoice_lines", "payments": "acct_invoice_payments",
        "line_fk": "invoice_id", "payment_fk": "invoice_id", "id_field": "invoice_id",
        "party": "customer", "party_fk": "customer_id", "party_table": "acct_customers",
        "number": "invoice_number", "source": "invoice", "payment_source": "invoice_payment",
        "role": "accounts_receivable", "money_column": "deposit_account_id",
        # On an invoice the control account is debited and the lines are credited.
        "control_side": "debit", "line_types": ("income",),
        "post_command": "send_invoice", "void_command": "void_invoice",
        "payment_command": "record_invoice_payment", "payment_word": "receipt",
    },
    "bill": {
        "table": "acct_bills", "lines": "acct_bill_lines", "payments": "acct_bill_payments",
        "line_fk": "bill_id", "payment_fk": "bill_id", "id_field": "bill_id",
        "party": "supplier", "party_fk": "supplier_id", "party_table": "acct_suppliers",
        "number": "bill_number", "source": "bill", "payment_source": "bill_payment",
        "role": "accounts_payable", "money_column": "paid_from_account_id",
        "control_side": "credit", "line_types": ("expense", "asset"),
        "post_command": "receive_bill", "void_command": "void_bill",
        "payment_command": "record_bill_payment", "payment_word": "payment",
    },
}


def _insert_document(conn, kind, lines, idempotency_key, party_id, number, issue_date, due_date, terms, notes):
    """Writes the lines, then the document row that closes them (the database checks the total against
    the lines, exactly as it checks a journal entry against its own)."""
    k = _KINDS[kind]
    resolved = [_resolve_line(conn, line, types=k["line_types"]) for line in lines]
    total = sum(line["amount_cents"] for line in resolved)
    if total <= 0:
        raise ValueError(f"a {kind} must come to more than nothing, got {total} cents")
    if total > MAX_CENTS:
        raise ValueError(f"a {kind} must be at most {MAX_CENTS} cents, got {total}")
    document_id = _next_id(conn, k["table"])
    for line_no, line in enumerate(resolved, start=1):
        conn.execute(f"""INSERT INTO {k['lines']} ({k['line_fk']}, line_no, description, quantity,
                                                   unit_price_cents, amount_cents, account_id)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (document_id, line_no, line["description"], line["quantity"], line["unit_price_cents"],
                      line["amount_cents"], line["account_id"]))
    conn.execute(f"""INSERT INTO {k['table']} (id, {k['party_fk']}, {k['number']}, issue_date, due_date,
                                               total_cents, terms, notes, idempotency_key)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (document_id, party_id, number, issue_date, due_date, total, terms, notes, idempotency_key))
    return document_id


def _post_document(conn, kind, document_id, idempotency_key, entry_date):
    """send_invoice / receive_bill: the draft's one journal entry, then the move to 'sent'."""
    k = _KINDS[kind]
    _positive_int(f"{kind}_id", document_id)
    if entry_date is not None:
        _date("entry_date", entry_date)

    def work():
        row = _document_row(conn, kind, document_id)
        if row["status"] != "draft":
            raise InvalidState(f"{kind} {document_id} is {row['status']}, so it is already in the books "
                               f"(or out of them)")
        control = _control_account(conn, k["role"])
        dated = entry_date or row["issue_date"]
        party = conn.execute(f"SELECT name FROM {k['party_table']} WHERE id = ?",
                             (row[k["party_fk"]],)).fetchone()["name"]
        memo = (f"Invoice {row[k['number']]} to {party}" if kind == "invoice"
                else f"Bill {row[k['number']]} from {party}")
        # One line per account, not per document line, so an invoice with three lines on one income
        # account becomes one credit. Ordered by account id, so the same invoice always posts the same entry.
        shares = conn.execute(f"""SELECT account_id, SUM(amount_cents) AS amount FROM {k['lines']}
                                  WHERE {k['line_fk']} = ? GROUP BY account_id ORDER BY account_id""",
                              (document_id,)).fetchall()
        side = k["control_side"]
        other = "credit" if side == "debit" else "debit"
        entry_lines = [_entry_line(control["id"], side, row["total_cents"])]
        # A waived line (a zero price on its own account) stays on the document but is not a journal
        # line: both of a line's amounts would be 0, which the journal refuses. It adds nothing to the
        # total, so leaving it out keeps the entry balanced.
        entry_lines += [_entry_line(s["account_id"], other, s["amount"]) for s in shares if s["amount"]]
        entry_id = _insert_entry(conn, dated, memo, k["source"], row[k["number"]],
                                 [_resolve_line(conn, line) for line in entry_lines], idempotency_key)
        conn.execute(f"UPDATE {k['table']} SET entry_id = ?, status = 'sent' WHERE id = ?",
                     (entry_id, document_id))
        return _document(conn, kind, document_id)

    payload = {k["id_field"]: document_id, "entry_date": entry_date}
    return _run(conn, idempotency_key, k["post_command"], payload, work)


def _record_payment(conn, kind, document_id, amount_cents, payment_date, money_account_id, idempotency_key,
                    reference):
    k = _KINDS[kind]
    _positive_int(f"{kind}_id", document_id)
    _cents("amount_cents", amount_cents)
    _date("payment_date", payment_date)
    _positive_int(k["money_column"], money_account_id)
    if reference is not None:
        reference = _clean_text("reference", reference)

    def work():
        row = _document_row(conn, kind, document_id)
        if row["status"] != "sent":
            raise InvalidState(f"{kind} {document_id} is {row['status']}: a {k['payment_word']} is recorded "
                               f"against a {kind} that is in the books and not yet settled")
        posted_on = conn.execute("SELECT entry_date FROM acct_journal_entries WHERE id = ?",
                                 (row["entry_id"],)).fetchone()["entry_date"]
        if payment_date < posted_on:
            raise ValueError(f"payment_date {payment_date} is before the entry date {posted_on}")
        outstanding = row["total_cents"] - row["amount_paid_cents"]
        if amount_cents > outstanding:
            raise InvalidState(f"{kind} {document_id} has {outstanding} cents outstanding, "
                               f"and the {k['payment_word']} is {amount_cents}")
        control = _control_account(conn, k["role"])
        money = _account_row(conn, money_account_id)
        if money["account_type"] != "asset" or money["role"] is not None or not money["is_active"]:
            raise InvalidState(f"account {money['code']} is not an active asset account that money can "
                               f"sit in (it is a {'closed ' if not money['is_active'] else ''}"
                               f"{money['account_type']} account)")
        # On a receipt the control account is credited and the money account debited; the other way for a payment.
        control_side = "credit" if k["control_side"] == "debit" else "debit"
        money_side = "debit" if control_side == "credit" else "credit"
        memo = (f"{k['payment_word'].capitalize()} for "
                f"{'invoice' if kind == 'invoice' else 'bill'} {row[k['number']]}")
        entry_lines = [_entry_line(money["id"], money_side, amount_cents),
                       _entry_line(control["id"], control_side, amount_cents)]
        entry_id = _insert_entry(conn, payment_date, memo, k["payment_source"], row[k["number"]],
                                 [_resolve_line(conn, line) for line in entry_lines], idempotency_key)
        conn.execute(f"""INSERT INTO {k['payments']} ({k['payment_fk']}, payment_date, amount_cents,
                                                      {k['money_column']}, reference, entry_id, idempotency_key)
                         VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (document_id, payment_date, amount_cents, money["id"], reference, entry_id,
                      idempotency_key))
        paid = row["amount_paid_cents"] + amount_cents
        conn.execute(f"UPDATE {k['table']} SET amount_paid_cents = ?, status = ? WHERE id = ?",
                     (paid, "paid" if paid == row["total_cents"] else "sent", document_id))
        return _document(conn, kind, document_id)

    payload = {k["id_field"]: document_id, "amount_cents": amount_cents, "payment_date": payment_date,
               k["money_column"]: money_account_id, "reference": reference}
    return _run(conn, idempotency_key, k["payment_command"], payload, work)


def _void_document(conn, kind, document_id, reason, idempotency_key, entry_date):
    k = _KINDS[kind]
    _positive_int(f"{kind}_id", document_id)
    reason = _clean_text("reason", reason, MAX_LONG_TEXT)
    if entry_date is not None:
        _date("entry_date", entry_date)

    def work():
        row = _document_row(conn, kind, document_id)
        if row["status"] == "void":
            raise InvalidState(f"{kind} {document_id} is already void")
        if row["amount_paid_cents"]:
            raise InvalidState(f"{kind} {document_id} has {row['amount_paid_cents']} cents paid against it: "
                               f"a {kind} with money against it is not voided")
        if row["status"] == "draft":
            # A draft was never in the books, so there is nothing to reverse.
            conn.execute(f"UPDATE {k['table']} SET status = 'void' WHERE id = ?", (document_id,))
        else:
            posted_on = conn.execute("SELECT entry_date FROM acct_journal_entries WHERE id = ?",
                                     (row["entry_id"],)).fetchone()["entry_date"]
            if entry_date is not None and entry_date < posted_on:
                raise ValueError(f"entry_date {entry_date} is before the entry date {posted_on}")
            memo = f"Void of {kind} {row[k['number']]}: {reason}"
            reversal = _insert_reversal(conn, row["entry_id"], idempotency_key, entry_date, memo,
                                        from_document=True)
            conn.execute(f"UPDATE {k['table']} SET status = 'void', void_entry_id = ? WHERE id = ?",
                         (reversal, document_id))
        return _document(conn, kind, document_id)

    payload = {k["id_field"]: document_id, "reason": reason, "entry_date": entry_date}
    return _run(conn, idempotency_key, k["void_command"], payload, work)


def _document_row(conn, kind, document_id):
    k = _KINDS[kind]
    _lookup_id(kind, document_id)
    row = conn.execute(f"SELECT * FROM {k['table']} WHERE id = ?", (document_id,)).fetchone()
    if row is None:
        raise NotFound(f"no {kind} {document_id}")
    return row


def _document(conn, kind, document_id, as_of=None):
    k = _KINDS[kind]
    as_of = _as_of(as_of)
    row = _document_row(conn, kind, document_id)
    lines = conn.execute(f"""SELECT l.line_no, l.description, l.quantity, l.unit_price_cents, l.amount_cents,
                                    l.account_id, a.code AS account_code, a.name AS account_name
                             FROM {k['lines']} l JOIN acct_accounts a ON a.id = l.account_id
                             WHERE l.{k['line_fk']} = ? ORDER BY l.line_no""", (document_id,))
    payments = conn.execute(f"""SELECT id AS payment_id, payment_date, amount_cents, {k['money_column']},
                                       reference, entry_id
                                FROM {k['payments']} WHERE {k['payment_fk']} = ? ORDER BY id""", (document_id,))
    return _document_dict(conn, kind, row, as_of) | {"lines": [dict(r) for r in lines],
                                                     "payments": [dict(r) for r in payments]}


def _documents(conn, kind, party_id, status, as_of, open_only):
    k = _KINDS[kind]
    as_of = _as_of(as_of)
    _bool("open_only", open_only)
    if status is not None and status not in DOCUMENT_STATUSES + ("overdue",):
        raise ValueError(f"status must be one of {', '.join(DOCUMENT_STATUSES)} or 'overdue', got {status!r}")
    sql, args = f"SELECT * FROM {k['table']} WHERE 1 = 1", []
    if party_id is not None:
        try:
            _lookup_id(k["party"], party_id)
        except NotFound:
            return []
        sql, args = sql + f" AND {k['party_fk']} = ?", args + [party_id]
    if status is not None and status != "overdue":
        sql, args = sql + " AND status = ?", args + [status]
    if open_only:
        sql += " AND status = 'sent'"
    out = [_document_dict(conn, kind, r, as_of) for r in conn.execute(sql + " ORDER BY due_date, id", args)]
    if status == "overdue":
        out = [d for d in out if d["effective_status"] == "overdue"]
    return out


def _document_dict(conn, kind, row, as_of):
    k = _KINDS[kind]
    party = conn.execute(f"SELECT name FROM {k['party_table']} WHERE id = ?", (row[k["party_fk"]],)).fetchone()
    outstanding = row["total_cents"] - row["amount_paid_cents"] if row["status"] in ("sent", "paid") else 0
    return {k["id_field"]: row["id"], k["party_fk"]: row[k["party_fk"]], k["party"]: party["name"],
            k["number"]: row[k["number"]], "issue_date": row["issue_date"], "due_date": row["due_date"],
            "status": row["status"], "effective_status": _effective_status(row, as_of),
            "total_cents": row["total_cents"], "amount_paid_cents": row["amount_paid_cents"],
            "outstanding_cents": outstanding, "terms": row["terms"], "notes": row["notes"],
            "entry_id": row["entry_id"], "void_entry_id": row["void_entry_id"], "as_of": as_of}


def _effective_status(row, as_of) -> str:
    """The stored status, except that a document in the books with money still owed on it reads as
    'overdue' once its due date has gone by. Never stored: it would be stale the next morning."""
    if row["status"] == "sent" and row["due_date"] < as_of:
        return "overdue"
    return row["status"]


def _aging(conn, kind, as_of):
    k = _KINDS[kind]
    as_of = _as_of(as_of)
    # The document counts if its own entry is dated on or before as_of and the void, if any, came later;
    # its outstanding amount is the total less the payments dated on or before as_of. So the report is
    # what the books said on as_of, not what they say now.
    rows = conn.execute(f"""SELECT d.id, d.{k['number']} AS number, d.{k['party_fk']} AS party_id,
                                   p.name AS party_name, d.due_date, d.issue_date, d.status, d.total_cents,
                                   (d.total_cents - COALESCE((SELECT SUM(pay.amount_cents) FROM {k['payments']} pay
                                                               WHERE pay.{k['payment_fk']} = d.id
                                                                 AND pay.payment_date <= ?), 0)) AS outstanding
                            FROM {k['table']} d
                            JOIN {k['party_table']} p ON p.id = d.{k['party_fk']}
                            JOIN acct_journal_entries e ON e.id = d.entry_id
                            WHERE e.entry_date <= ?
                              AND (d.void_entry_id IS NULL
                                   OR (SELECT v.entry_date FROM acct_journal_entries v
                                        WHERE v.id = d.void_entry_id) > ?)
                            ORDER BY d.due_date, d.id""", (as_of, as_of, as_of)).fetchall()
    documents, by_party = [], {}
    for row in rows:
        if row["outstanding"] <= 0:
            continue
        late = _days_between(row["due_date"], as_of)
        bucket = _bucket(late)
        documents.append({k["id_field"]: row["id"], k["number"]: row["number"], k["party_fk"]: row["party_id"],
                          k["party"]: row["party_name"], "due_date": row["due_date"],
                          "outstanding_cents": row["outstanding"], "days_late": max(late, 0), "bucket": bucket})
        party = by_party.setdefault(row["party_id"], {k["party_fk"]: row["party_id"], k["party"]: row["party_name"],
                                                      **{b: 0 for b in _AGING_BUCKETS}, "total_cents": 0})
        party[bucket] += row["outstanding"]
        party["total_cents"] += row["outstanding"]
    totals = {b: sum(p[b] for p in by_party.values()) for b in _AGING_BUCKETS}
    totals["total_cents"] = sum(p["total_cents"] for p in by_party.values())
    return {"as_of": as_of, "documents": documents,
            "by_" + k["party"]: sorted(by_party.values(), key=lambda p: p[k["party"]].lower()),
            "totals": totals}


def _bucket(days_late: int) -> str:
    if days_late <= 0:
        return "current"
    if days_late <= 30:
        return "1_30"
    if days_late <= 60:
        return "31_60"
    if days_late <= 90:
        return "61_90"
    return "over_90"


# ---------------------------------------------------------------- internals: the journal

def _entry_line(account_id, side, amount_cents, memo=None) -> dict:
    """One line of an entry the module posts itself: a debit or a credit, with the other side at zero."""
    return {"account_id": account_id, "memo": memo,
            "debit_cents": amount_cents if side == "debit" else 0,
            "credit_cents": amount_cents if side == "credit" else 0}


def _insert_entry(conn, entry_date, memo, source, source_ref, lines, idempotency_key, reverses_id=None) -> int:
    """Writes the lines, then the entry row that closes them. The database checks that debits equal
    credits and that both equal the total (migration 0001), so this can't leave the books unbalanced
    even if the caller's arithmetic is wrong."""
    debits = sum(line["debit_cents"] for line in lines)
    credits = sum(line["credit_cents"] for line in lines)
    if debits != credits:
        raise Unbalanced(f"debits ({debits} cents) must equal credits ({credits} cents)")
    if debits == 0:
        raise ValueError("a journal entry must move more than nothing")
    if debits > MAX_CENTS:
        raise ValueError(f"an entry must be at most {MAX_CENTS} cents, got {debits}")
    # The entry row comes last (see 0001), so its id is chosen here. BEGIN IMMEDIATE holds the write
    # lock, so no other writer can take it meanwhile.
    entry_id = _next_id(conn, "acct_journal_entries")
    for line_no, line in enumerate(lines, start=1):
        conn.execute("""INSERT INTO acct_journal_lines (entry_id, line_no, account_id, debit_cents,
                                                        credit_cents, memo)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                     (entry_id, line_no, line["account_id"], line["debit_cents"], line["credit_cents"],
                      line["memo"]))
    conn.execute("""INSERT INTO acct_journal_entries (id, entry_date, memo, source, source_ref, total_cents,
                                                      reverses_id, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                 (entry_id, entry_date, memo, source, source_ref, debits, reverses_id, idempotency_key))
    return entry_id


def _insert_reversal(conn, entry_id, idempotency_key, entry_date=None, memo=None, *, from_document=False) -> int:
    """The mirror image of an entry: the same lines in the same order with debits and credits swapped.

    from_document is set only by voiding an invoice or a bill, which is what reverses those entries.
    reverse_journal_entry leaves it false, so a document or payment entry can't be reversed on its own.
    """
    original = conn.execute("SELECT * FROM acct_journal_entries WHERE id = ?", (entry_id,)).fetchone()
    if original is None:
        raise NotFound(f"no journal entry {entry_id}")
    if not from_document and original["source"] not in MANUAL_SOURCES:
        raise InvalidState(f"entry {entry_id} was posted by {original['source']}; "
                           f"only a manual or payroll entry can be reversed here")
    already = conn.execute("SELECT id FROM acct_journal_entries WHERE reverses_id = ?", (entry_id,)).fetchone()
    if already:
        raise InvalidState(f"entry {entry_id} was already reversed by entry {already['id']}; "
                           f"reverse entry {already['id']} instead")
    lines = conn.execute("""SELECT l.line_no, l.account_id, l.debit_cents, l.credit_cents, l.memo, a.code,
                                   a.is_active
                            FROM acct_journal_lines l JOIN acct_accounts a ON a.id = l.account_id
                            WHERE l.entry_id = ? ORDER BY l.line_no""", (entry_id,)).fetchall()
    closed = [line["code"] for line in lines if not line["is_active"]]
    if closed:
        raise InvalidState(f"entry {entry_id} posts to closed account(s) {', '.join(closed)}: "
                           f"reopen them before reversing it")
    mirrored = [{"account_id": line["account_id"], "debit_cents": line["credit_cents"],
                 "credit_cents": line["debit_cents"], "memo": line["memo"]} for line in lines]
    return _insert_entry(conn, entry_date or original["entry_date"],
                         memo or f"Reversal of entry {entry_id}: {original['memo']}",
                         original["source"], original["source_ref"], mirrored, idempotency_key,
                         reverses_id=entry_id)


def _check_lines(lines) -> list[dict]:
    """Checks journal lines as given, without touching the database, and returns them cleaned (so a
    replayed command compares equal to the first one)."""
    if not isinstance(lines, list) or len(lines) < 2:
        raise ValueError("a journal entry needs at least two lines")
    if len(lines) > MAX_LINES:
        raise ValueError(f"a journal entry can have at most {MAX_LINES} lines, got {len(lines)}")
    out = []
    for i, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            raise ValueError(f"line {i} must be a dict, got {line!r}")
        unknown = set(line) - {"account_id", "account_code", "debit_cents", "credit_cents", "memo"}
        if unknown:
            raise ValueError(f"line {i} has unknown fields: {', '.join(sorted(unknown))}")
        debit = line.get("debit_cents") or 0
        credit = line.get("credit_cents") or 0
        if bool(debit) == bool(credit):
            raise ValueError(f"line {i} must be a debit or a credit, not both and not nothing")
        _cents("debit_cents" if debit else "credit_cents", debit or credit)
        out.append({**_line_account(i, line), "debit_cents": debit, "credit_cents": credit,
                    "memo": None if line.get("memo") is None else _clean_text(f"line {i} memo", line["memo"],
                                                                             MAX_LONG_TEXT)})
    return out


def _check_document_lines(lines) -> list[dict]:
    """The same for an invoice's or a bill's lines, where an amount is a whole quantity times a unit price."""
    if not isinstance(lines, list) or not lines:
        raise ValueError("an invoice or bill needs at least one line")
    if len(lines) > MAX_LINES:
        raise ValueError(f"an invoice or bill can have at most {MAX_LINES} lines, got {len(lines)}")
    out = []
    for i, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            raise ValueError(f"line {i} must be a dict, got {line!r}")
        unknown = set(line) - {"account_id", "account_code", "description", "quantity", "unit_price_cents"}
        if unknown:
            raise ValueError(f"line {i} has unknown fields: {', '.join(sorted(unknown))}")
        quantity = line.get("quantity", 1)
        if type(quantity) is not int or not 0 < quantity <= MAX_QUANTITY:
            raise ValueError(f"line {i}: quantity must be a whole number of units from 1 to {MAX_QUANTITY}, "
                             f"got {quantity!r}; for part of a unit, say so in the description and give the "
                             f"line's whole amount as the unit price")
        _cents(f"line {i} unit_price_cents", line.get("unit_price_cents"), allow_zero=True)
        amount = quantity * line["unit_price_cents"]
        if amount > MAX_CENTS:
            raise ValueError(f"line {i} comes to {amount} cents, more than the {MAX_CENTS} allowed")
        out.append({**_line_account(i, line),
                    "description": _clean_text(f"line {i} description", line.get("description")),
                    "quantity": quantity, "unit_price_cents": line["unit_price_cents"], "amount_cents": amount})
    return out


def _line_account(i, line) -> dict:
    """Either 'account_id' or 'account_code', exactly one, left unresolved until the transaction is open."""
    has_id, has_code = line.get("account_id") is not None, line.get("account_code") is not None
    if has_id == has_code:
        raise ValueError(f"line {i} must name its account by account_id or account_code, not both and not neither")
    if has_id:
        _positive_int(f"line {i} account_id", line["account_id"])
        return {"account_id": line["account_id"]}
    return {"account_code": _clean_text(f"line {i} account_code", line["account_code"])}


def _resolve_line(conn, line, types=None) -> dict:
    """Looks the line's account up by id or code and checks it can be posted to."""
    if "account_id" in line:
        row = _account_row(conn, line["account_id"])
    else:
        row = conn.execute("SELECT * FROM acct_accounts WHERE code = ?", (line["account_code"],)).fetchone()
        if row is None:
            raise NotFound(f"no account with code {line['account_code']!r}")
    if not row["is_active"]:
        raise InvalidState(f"account {row['code']} ({row['name']}) is closed, so nothing is posted to it")
    if types is not None and row["account_type"] not in types:
        raise InvalidState(f"account {row['code']} ({row['name']}) is {_article(row['account_type'])} "
                           f"{row['account_type']} account; this line needs "
                           f"{' or '.join(types)}")
    if types is not None and row["role"] is not None:
        raise InvalidState(f"account {row['code']} is the {row['role']} account, which an invoice or bill "
                           f"line never posts to: it is what the document itself moves")
    return {key: value for key, value in line.items() if key != "account_code"} | {"account_id": row["id"]}


def _control_account(conn, role):
    row = conn.execute("SELECT * FROM acct_accounts WHERE role = ?", (role,)).fetchone()
    if row is None:
        raise InvalidState(f"this business has no {role} account yet: add one with "
                           f"add_account(..., role={role!r})")
    if not row["is_active"]:
        raise InvalidState(f"the {role} account ({row['code']}) is closed")
    return row


# ---------------------------------------------------------------- internals: accounts and balances

def _account_row(conn, account_id):
    _lookup_id("account", account_id)
    row = conn.execute("SELECT * FROM acct_accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise NotFound(f"no account {account_id}")
    return row


def _account_dict(conn, row, balances=None) -> dict:
    if balances is None:
        balances = _balances(conn, None)
    return balances.get(row["id"]) or _empty_balance(row)


def _balances(conn, as_of, from_date=None) -> dict:
    """{account_id: balance row} for every account with a journal line in the range. One query, so a
    report never walks the accounts one at a time."""
    sql = """SELECT a.id, a.code, a.name, a.account_type, a.role, a.is_active,
                    COALESCE(SUM(l.debit_cents), 0) AS debits, COALESCE(SUM(l.credit_cents), 0) AS credits
             FROM acct_accounts a
             JOIN acct_journal_lines l ON l.account_id = a.id
             JOIN acct_journal_entries e ON e.id = l.entry_id
             WHERE 1 = 1"""
    args = []
    if as_of is not None:
        sql, args = sql + " AND e.entry_date <= ?", args + [as_of]
    if from_date is not None:
        sql, args = sql + " AND e.entry_date >= ?", args + [from_date]
    out = {}
    for row in conn.execute(sql + " GROUP BY a.id", args):
        natural = (row["debits"] - row["credits"] if row["account_type"] in DEBIT_TYPES
                   else row["credits"] - row["debits"])
        out[row["id"]] = {"account_id": row["id"], "code": row["code"], "name": row["name"],
                          "account_type": row["account_type"], "role": row["role"],
                          "is_active": bool(row["is_active"]), "debit_cents": row["debits"],
                          "credit_cents": row["credits"], "balance_cents": natural}
    return out


def _empty_balance(row) -> dict:
    return {"account_id": row["id"], "code": row["code"], "name": row["name"],
            "account_type": row["account_type"], "role": row["role"], "is_active": bool(row["is_active"]),
            "debit_cents": 0, "credit_cents": 0, "balance_cents": 0}


# ---------------------------------------------------------------- internals: customers and suppliers

def _add_party(conn, kind, idempotency_key, name, fields):
    table = f"acct_{kind}s"
    unknown = set(fields) - set(PARTY_FIELDS) - {"name"}
    if unknown:
        raise ValueError(f"unknown {kind} fields: {', '.join(sorted(unknown))}")
    record = _clean_party({"name": name, **fields})

    def work():
        taken = conn.execute(f"SELECT id FROM {table} WHERE lower(name) = lower(?)", (record["name"],)).fetchone()
        if taken:
            raise InvalidState(f"{kind} {record['name']!r} is already {kind} {taken['id']}")
        columns = ", ".join(PARTY_FIELDS)
        marks = ", ".join("?" * len(PARTY_FIELDS))
        party_id = conn.execute(f"INSERT INTO {table} ({columns}, idempotency_key) VALUES ({marks}, ?)",
                                [record[f] for f in PARTY_FIELDS] + [idempotency_key]).lastrowid
        return _party_dict(_party_row(conn, kind, party_id))

    return _run(conn, idempotency_key, f"add_{kind}", record, work)


def _update_party(conn, kind, party_id, changes, idempotency_key):
    table = f"acct_{kind}s"
    _positive_int(f"{kind}_id", party_id)
    if not isinstance(changes, dict) or not changes:
        raise ValueError("changes must be a non-empty dict of fields")
    unknown = set(changes) - set(UPDATABLE_PARTY_FIELDS)
    if unknown:
        raise ValueError(f"these fields can't be updated: {', '.join(sorted(unknown))}")
    clean = _clean_party(changes, require_name=False)

    def work():
        current = _party_row(conn, kind, party_id)
        if "name" in clean:
            taken = conn.execute(f"SELECT id FROM {table} WHERE lower(name) = lower(?) AND id <> ?",
                                 (clean["name"], party_id)).fetchone()
            if taken:
                raise InvalidState(f"{kind} {clean['name']!r} is already {kind} {taken['id']}")
        if not any(current[f] != v for f, v in clean.items()):
            raise ValueError(f"nothing changes for {kind} {party_id}")
        sets = ", ".join(f"{f} = ?" for f in clean)
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", list(clean.values()) + [party_id])
        return _party_dict(_party_row(conn, kind, party_id))

    payload = {f"{kind}_id": party_id, "changes": clean}
    return _run(conn, idempotency_key, f"update_{kind}", payload, work)


def _clean_party(fields, require_name=True) -> dict:
    """The given party fields, cleaned. On add, every field is present (missing ones as None)."""
    out = {}
    for field in UPDATABLE_PARTY_FIELDS:
        if field not in fields:
            if require_name and field in PARTY_FIELDS:
                out[field] = 0 if field == "payment_terms_days" else None
            continue
        value = fields[field]
        if field == "payment_terms_days":
            if value is None:
                value = 0
            if type(value) is not int or not 0 <= value <= 3650:
                raise ValueError(f"payment_terms_days must be a whole number of days from 0 to 3650, got {value!r}")
        elif field == "is_active":
            _bool("is_active", value)
            value = int(value)
        elif field == "email":
            value = None if value is None else _clean_text("email", value).lower()
            if value is not None:
                local, _, domain = value.partition("@")
                if not local or not domain or " " in value:
                    raise ValueError(f"email must look like name@domain, got {value!r}")
        elif field in ("terms", "notes"):
            value = None if value is None else _clean_text(field, value, MAX_LONG_TEXT)
        elif field == "name":
            value = _clean_text("name", value)
        else:
            value = None if value is None else _clean_text(field, value)
        out[field] = value
    if require_name and not out.get("name"):
        raise ValueError("name is required")
    return out


def _party_row(conn, kind, party_id):
    _lookup_id(kind, party_id)
    row = conn.execute(f"SELECT * FROM acct_{kind}s WHERE id = ?", (party_id,)).fetchone()
    if row is None:
        raise NotFound(f"no {kind} {party_id}")
    return row


def _party_dict(row) -> dict:
    """The two tables have the same shape, so one function reads both."""
    return ({"id": row["id"]} | {f: row[f] for f in PARTY_FIELDS}
            | {"is_active": bool(row["is_active"])})


def _parties(conn, kind, active_only) -> list[dict]:
    _bool("active_only", active_only)
    sql = f"SELECT * FROM acct_{kind}s"
    if active_only:
        sql += " WHERE is_active = 1"
    return [_party_dict(r) for r in conn.execute(sql + " ORDER BY id")]


# ---------------------------------------------------------------- internals: commands and checks

def _run(conn, idempotency_key, command, payload, work):
    """Runs work() in one BEGIN IMMEDIATE transaction, or returns the stored result of an earlier run of
    this key. A failed command records nothing, so its key stays free."""
    _text("idempotency_key", idempotency_key)
    body = json.dumps(payload, sort_keys=True)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT command, payload, result FROM acct_commands WHERE idempotency_key = ?",
                           (idempotency_key,)).fetchone()
        if row is not None:
            if (row["command"], row["payload"]) != (command, body):
                raise IdempotencyConflict(f"key {idempotency_key!r} was already used for "
                                          f"{row['command']} {row['payload']}")
            result = row["result"]
        else:
            # A command writes its key onto the rows it creates (an entry, an invoice, a payment), which
            # have no acct_commands row of their own, so a later command can't reuse one of those keys.
            if _key_taken(conn, idempotency_key):
                raise IdempotencyConflict(f"key {idempotency_key!r} was already used inside an earlier command")
            result = json.dumps(work(), sort_keys=True)
            conn.execute("INSERT INTO acct_commands (idempotency_key, command, payload, result) "
                         "VALUES (?, ?, ?, ?)", (idempotency_key, command, body, result))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return json.loads(result)


_KEYED_TABLES = ("acct_commands", "acct_accounts", "acct_journal_entries", "acct_customers", "acct_suppliers",
                 "acct_invoices", "acct_invoice_payments", "acct_bills", "acct_bill_payments")


def _key_taken(conn, key) -> bool:
    """Whether any row a command wrote already carries this key. Only the tables this schema version has
    are looked at (the documents arrive with migration 0002)."""
    marks = ", ".join("?" * len(_KEYED_TABLES))
    tables = [t for (t,) in conn.execute(f"""SELECT name FROM sqlite_master WHERE type = 'table'
                                              AND name IN ({marks})""", _KEYED_TABLES)]
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE idempotency_key = ?", (key,)).fetchone() for t in tables)


def _next_id(conn, table) -> int:
    """The id the next row of table gets. Only called with the write lock held, and only for the tables
    whose rows are written after the rows that point at them."""
    return conn.execute(f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}").fetchone()[0]


def _as_of(value) -> str:
    """The date a query reports on: the caller's, or the machine's today. Never stored, and never part of
    a command's payload, so a replay can't depend on it."""
    if value is None:
        return datetime.date.today().isoformat()
    _date("as_of", value)
    return value


def _add_days(date_text: str, days: int) -> str:
    return (datetime.date.fromisoformat(date_text) + datetime.timedelta(days=days)).isoformat()


def _days_between(start: str, end: str) -> int:
    return (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(start)).days


def _article(word: str) -> str:
    return "an" if word[:1] in "aeiou" else "a"


def _clean(value) -> str:
    """NFC, surrounding whitespace trimmed and inner whitespace collapsed to one space."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _clean_text(name, value, limit=MAX_TEXT) -> str:
    _text(name, value, limit)
    cleaned = _clean(value)
    if not cleaned:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    return cleaned


def _text(name, value, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    if len(value) > limit:
        raise ValueError(f"{name} must be at most {limit} characters, got {len(value)}")


def _positive_int(name, value):
    if type(value) is not int or not 0 < value <= MAX_ID:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _bool(name, value):
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false, got {value!r}")


def _cents(name, value, allow_zero=False):
    """An amount of money: whole cents, never a float, so nothing rounds."""
    if type(value) is not int:
        raise ValueError(f"{name} must be a whole number of cents, got {value!r}")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'at least 0' if allow_zero else 'more than 0'} cents, got {value}")
    if value > MAX_CENTS:
        raise ValueError(f"{name} must be at most {MAX_CENTS} cents, got {value}")


def _lookup_id(what, value):
    """An id to read by: not an integer is a ValueError; one no row can have (0, negative, past 2**63-1) is
    NotFound, never sqlite3's OverflowError."""
    if type(value) is not int:
        raise ValueError(f"{what} id must be an integer, got {value!r}")
    if not 0 < value <= MAX_ID:
        raise NotFound(f"no {what} {value}")


def _date(name, value):
    try:
        ok = isinstance(value, str) and datetime.date.fromisoformat(value).isoformat() == value
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(f"{name} must be a date as 'YYYY-MM-DD', got {value!r}")
