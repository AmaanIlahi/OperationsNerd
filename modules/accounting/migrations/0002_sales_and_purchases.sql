-- Accounting module, migration 0002: customers and suppliers, invoices and bills
-- with their line items, and the payments that settle them.
--
-- Nothing here holds a balance of its own. An invoice or a bill is a document;
-- what it does to the books is the journal entry it points at, and every
-- figure a report shows comes from acct_journal_lines (0001). The documents
-- exist so a person can see what the entry was for, and so the aging reports
-- know when money is due.
--
-- Money is whole cents in INTEGER columns, as in 0001, and the same date CHECK
-- (`x IS date(x)` plus the day-of-month guard) is used on every date, so
-- impossible days such as 2026-02-30 are refused on every SQLite version.
--
-- Documents are append-only in the same way the journal is. A line is written
-- once; the document row is written once and afterwards only its status,
-- amount_paid_cents, entry_id and void_entry_id ever change. There is no edit:
-- a wrong invoice is voided (which reverses its entry) and raised again.
--
-- Lines are inserted before their document row, exactly as journal lines are,
-- so the row that closes a document can be checked against the lines that are
-- already there: the total equals the sum of the lines and there is at least
-- one. A line inserted after its document row exists is refused, so a document
-- that exists can never stop matching its lines.
--
-- 'overdue' is not a status here. Whether a document is overdue depends on what
-- day it is, so storing it would mean a row that silently goes stale; the
-- reports and queries in accounting.py derive it from due_date and the caller's
-- as_of date instead (README, "Statuses").

INSERT INTO acct_command_names (name) VALUES
    ('add_customer'), ('update_customer'), ('add_supplier'), ('update_supplier'),
    ('create_invoice'), ('send_invoice'), ('record_invoice_payment'), ('void_invoice'),
    ('create_bill'), ('receive_bill'), ('record_bill_payment'), ('void_bill');

-- What a journal entry was raised for. 0001 has 'manual' and the unwired
-- 'payroll' hook; these four are written by the commands below.
INSERT INTO acct_entry_sources (name) VALUES
    ('invoice'), ('bill'), ('invoice_payment'), ('bill_payment');

-- ---------------------------------------------------------------- the people on either side

CREATE TABLE acct_customers (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL CHECK (name <> ''),
    email              TEXT    CHECK (email <> ''),
    phone              TEXT    CHECK (phone <> ''),
    billing_address    TEXT    CHECK (billing_address <> ''),
    -- How many days after the issue date an invoice falls due, by default.
    payment_terms_days INTEGER NOT NULL DEFAULT 0 CHECK (payment_terms_days BETWEEN 0 AND 3650),
    -- The only trace of an agreement this module keeps: free text a person wrote
    -- (payment terms, a contract reference). Nothing reads it (README, "Out of scope").
    terms              TEXT    CHECK (terms <> ''),
    notes              TEXT    CHECK (notes <> ''),
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    idempotency_key    TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

CREATE UNIQUE INDEX acct_customers_name ON acct_customers (lower(name));

CREATE TRIGGER acct_customers_fixed_columns BEFORE UPDATE ON acct_customers
WHEN NEW.id IS NOT OLD.id OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a customer''s id and key never change'); END;

CREATE TRIGGER acct_customers_no_delete BEFORE DELETE ON acct_customers
BEGIN SELECT RAISE(ABORT, 'customers are kept: invoices refer to them'); END;

CREATE TABLE acct_suppliers (
    id                 INTEGER PRIMARY KEY,
    name               TEXT    NOT NULL CHECK (name <> ''),
    email              TEXT    CHECK (email <> ''),
    phone              TEXT    CHECK (phone <> ''),
    billing_address    TEXT    CHECK (billing_address <> ''),
    payment_terms_days INTEGER NOT NULL DEFAULT 0 CHECK (payment_terms_days BETWEEN 0 AND 3650),
    terms              TEXT    CHECK (terms <> ''),
    notes              TEXT    CHECK (notes <> ''),
    is_active          INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    idempotency_key    TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

CREATE UNIQUE INDEX acct_suppliers_name ON acct_suppliers (lower(name));

CREATE TRIGGER acct_suppliers_fixed_columns BEFORE UPDATE ON acct_suppliers
WHEN NEW.id IS NOT OLD.id OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a supplier''s id and key never change'); END;

CREATE TRIGGER acct_suppliers_no_delete BEFORE DELETE ON acct_suppliers
BEGIN SELECT RAISE(ABORT, 'suppliers are kept: bills refer to them'); END;

-- ---------------------------------------------------------------- invoices (money owed to us)

CREATE TABLE acct_invoices (
    id                INTEGER PRIMARY KEY,
    customer_id       INTEGER NOT NULL REFERENCES acct_customers (id),
    invoice_number    TEXT    NOT NULL CHECK (invoice_number <> ''),
    issue_date        TEXT    NOT NULL CHECK (issue_date IS date(issue_date) AND substr(issue_date, 9, 2) <= substr(date(substr(issue_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    due_date          TEXT    NOT NULL CHECK (due_date IS date(due_date) AND substr(due_date, 9, 2) <= substr(date(substr(due_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    status            TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'paid', 'void')),
    total_cents       INTEGER NOT NULL CHECK (total_cents > 0 AND total_cents <= 10000000000000),
    amount_paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
    terms             TEXT    CHECK (terms <> ''),
    notes             TEXT    CHECK (notes <> ''),
    -- The entry that put this invoice in the books, written when it is sent.
    entry_id          INTEGER REFERENCES acct_journal_entries (id),
    -- The reversing entry written when it is voided.
    void_entry_id     INTEGER REFERENCES acct_journal_entries (id),
    idempotency_key   TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (invoice_number),
    UNIQUE (idempotency_key),
    CHECK (due_date >= issue_date),
    CHECK (amount_paid_cents <= total_cents),
    -- A draft is not in the books yet; sending it puts it there, voiding it takes it
    -- back out through a reversal. A draft that is thrown away is voided as well
    -- (nothing here is deleted), and it has no entry to reverse.
    CHECK (status <> 'draft' OR entry_id IS NULL),
    CHECK (status IN ('draft', 'void') OR entry_id IS NOT NULL),
    CHECK ((status = 'void' AND entry_id IS NOT NULL) = (void_entry_id IS NOT NULL)),
    -- Nothing is paid until the invoice is sent, and 'paid' means paid in full.
    CHECK (status <> 'draft' OR amount_paid_cents = 0),
    CHECK ((status = 'paid') = (status IN ('paid', 'sent') AND amount_paid_cents = total_cents))
);

CREATE INDEX acct_invoices_customer ON acct_invoices (customer_id);
CREATE INDEX acct_invoices_due ON acct_invoices (status, due_date);

CREATE TABLE acct_invoice_lines (
    id              INTEGER PRIMARY KEY,
    -- The invoice row is written after its lines (see the header), so this is checked at COMMIT.
    invoice_id      INTEGER NOT NULL REFERENCES acct_invoices (id) DEFERRABLE INITIALLY DEFERRED,
    line_no         INTEGER NOT NULL CHECK (line_no >= 1),
    description     TEXT    NOT NULL CHECK (description <> ''),
    -- A whole number of units, so quantity * unit price is exact: no rounding, ever (README, "Known limits").
    quantity        INTEGER NOT NULL CHECK (quantity > 0 AND quantity <= 1000000),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0 AND unit_price_cents <= 10000000000000),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0 AND amount_cents <= 10000000000000),
    -- Which income account the sale lands in.
    account_id      INTEGER NOT NULL REFERENCES acct_accounts (id),
    UNIQUE (invoice_id, line_no),
    CHECK (amount_cents = quantity * unit_price_cents)
);

CREATE INDEX acct_invoice_lines_account ON acct_invoice_lines (account_id);

CREATE TABLE acct_invoice_payments (
    id              INTEGER PRIMARY KEY,
    invoice_id      INTEGER NOT NULL REFERENCES acct_invoices (id),
    payment_date    TEXT    NOT NULL CHECK (payment_date IS date(payment_date) AND substr(payment_date, 9, 2) <= substr(date(substr(payment_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0 AND amount_cents <= 10000000000000),
    -- Where the money arrived: the asset account that is debited.
    deposit_account_id INTEGER NOT NULL REFERENCES acct_accounts (id),
    reference       TEXT    CHECK (reference <> ''),
    entry_id        INTEGER NOT NULL REFERENCES acct_journal_entries (id),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key),
    UNIQUE (entry_id)
);

CREATE INDEX acct_invoice_payments_invoice ON acct_invoice_payments (invoice_id);

-- ---------------------------------------------------------------- bills (money we owe)

CREATE TABLE acct_bills (
    id                INTEGER PRIMARY KEY,
    supplier_id       INTEGER NOT NULL REFERENCES acct_suppliers (id),
    -- The supplier's own number, so two suppliers may use the same one.
    bill_number       TEXT    NOT NULL CHECK (bill_number <> ''),
    issue_date        TEXT    NOT NULL CHECK (issue_date IS date(issue_date) AND substr(issue_date, 9, 2) <= substr(date(substr(issue_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    due_date          TEXT    NOT NULL CHECK (due_date IS date(due_date) AND substr(due_date, 9, 2) <= substr(date(substr(due_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    status            TEXT    NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'paid', 'void')),
    total_cents       INTEGER NOT NULL CHECK (total_cents > 0 AND total_cents <= 10000000000000),
    amount_paid_cents INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid_cents >= 0),
    terms             TEXT    CHECK (terms <> ''),
    notes             TEXT    CHECK (notes <> ''),
    entry_id          INTEGER REFERENCES acct_journal_entries (id),
    void_entry_id     INTEGER REFERENCES acct_journal_entries (id),
    idempotency_key   TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (supplier_id, bill_number),
    UNIQUE (idempotency_key),
    CHECK (due_date >= issue_date),
    CHECK (amount_paid_cents <= total_cents),
    CHECK (status <> 'draft' OR entry_id IS NULL),
    CHECK (status IN ('draft', 'void') OR entry_id IS NOT NULL),
    CHECK ((status = 'void' AND entry_id IS NOT NULL) = (void_entry_id IS NOT NULL)),
    CHECK (status <> 'draft' OR amount_paid_cents = 0),
    CHECK ((status = 'paid') = (status IN ('paid', 'sent') AND amount_paid_cents = total_cents))
);

CREATE INDEX acct_bills_supplier ON acct_bills (supplier_id);
CREATE INDEX acct_bills_due ON acct_bills (status, due_date);

CREATE TABLE acct_bill_lines (
    id              INTEGER PRIMARY KEY,
    bill_id         INTEGER NOT NULL REFERENCES acct_bills (id) DEFERRABLE INITIALLY DEFERRED,
    line_no         INTEGER NOT NULL CHECK (line_no >= 1),
    description     TEXT    NOT NULL CHECK (description <> ''),
    quantity        INTEGER NOT NULL CHECK (quantity > 0 AND quantity <= 1000000),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0 AND unit_price_cents <= 10000000000000),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents >= 0 AND amount_cents <= 10000000000000),
    -- Which expense (or asset) account the purchase lands in.
    account_id      INTEGER NOT NULL REFERENCES acct_accounts (id),
    UNIQUE (bill_id, line_no),
    CHECK (amount_cents = quantity * unit_price_cents)
);

CREATE INDEX acct_bill_lines_account ON acct_bill_lines (account_id);

CREATE TABLE acct_bill_payments (
    id              INTEGER PRIMARY KEY,
    bill_id         INTEGER NOT NULL REFERENCES acct_bills (id),
    payment_date    TEXT    NOT NULL CHECK (payment_date IS date(payment_date) AND substr(payment_date, 9, 2) <= substr(date(substr(payment_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0 AND amount_cents <= 10000000000000),
    -- Where the money left from: the asset account that is credited.
    paid_from_account_id INTEGER NOT NULL REFERENCES acct_accounts (id),
    reference       TEXT    CHECK (reference <> ''),
    entry_id        INTEGER NOT NULL REFERENCES acct_journal_entries (id),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key),
    UNIQUE (entry_id)
);

CREATE INDEX acct_bill_payments_bill ON acct_bill_payments (bill_id);

-- ---------------------------------------------------------------- what can never change

CREATE TRIGGER acct_invoices_fixed_columns BEFORE UPDATE ON acct_invoices
WHEN NEW.id IS NOT OLD.id OR NEW.customer_id IS NOT OLD.customer_id
  OR NEW.invoice_number IS NOT OLD.invoice_number OR NEW.issue_date IS NOT OLD.issue_date
  OR NEW.due_date IS NOT OLD.due_date OR NEW.total_cents IS NOT OLD.total_cents
  OR NEW.terms IS NOT OLD.terms OR NEW.notes IS NOT OLD.notes
  OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'an invoice is not edited: void it and raise it again'); END;

CREATE TRIGGER acct_invoices_no_delete BEFORE DELETE ON acct_invoices
BEGIN SELECT RAISE(ABORT, 'invoices are kept: void one instead of deleting it'); END;

CREATE TRIGGER acct_bills_fixed_columns BEFORE UPDATE ON acct_bills
WHEN NEW.id IS NOT OLD.id OR NEW.supplier_id IS NOT OLD.supplier_id
  OR NEW.bill_number IS NOT OLD.bill_number OR NEW.issue_date IS NOT OLD.issue_date
  OR NEW.due_date IS NOT OLD.due_date OR NEW.total_cents IS NOT OLD.total_cents
  OR NEW.terms IS NOT OLD.terms OR NEW.notes IS NOT OLD.notes
  OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a bill is not edited: void it and enter it again'); END;

CREATE TRIGGER acct_bills_no_delete BEFORE DELETE ON acct_bills
BEGIN SELECT RAISE(ABORT, 'bills are kept: void one instead of deleting it'); END;

CREATE TRIGGER acct_invoice_lines_no_update BEFORE UPDATE ON acct_invoice_lines
BEGIN SELECT RAISE(ABORT, 'invoice lines are fixed: void the invoice and raise it again'); END;

CREATE TRIGGER acct_invoice_lines_no_delete BEFORE DELETE ON acct_invoice_lines
BEGIN SELECT RAISE(ABORT, 'invoice lines are fixed: void the invoice and raise it again'); END;

CREATE TRIGGER acct_bill_lines_no_update BEFORE UPDATE ON acct_bill_lines
BEGIN SELECT RAISE(ABORT, 'bill lines are fixed: void the bill and enter it again'); END;

CREATE TRIGGER acct_bill_lines_no_delete BEFORE DELETE ON acct_bill_lines
BEGIN SELECT RAISE(ABORT, 'bill lines are fixed: void the bill and enter it again'); END;

CREATE TRIGGER acct_invoice_payments_no_update BEFORE UPDATE ON acct_invoice_payments
BEGIN SELECT RAISE(ABORT, 'a payment is not edited: reverse its journal entry instead'); END;

CREATE TRIGGER acct_invoice_payments_no_delete BEFORE DELETE ON acct_invoice_payments
BEGIN SELECT RAISE(ABORT, 'a payment is not deleted: reverse its journal entry instead'); END;

CREATE TRIGGER acct_bill_payments_no_update BEFORE UPDATE ON acct_bill_payments
BEGIN SELECT RAISE(ABORT, 'a payment is not edited: reverse its journal entry instead'); END;

CREATE TRIGGER acct_bill_payments_no_delete BEFORE DELETE ON acct_bill_payments
BEGIN SELECT RAISE(ABORT, 'a payment is not deleted: reverse its journal entry instead'); END;

-- ---------------------------------------------------------------- a document matches its lines

CREATE TRIGGER acct_invoices_total_matches_lines BEFORE INSERT ON acct_invoices
WHEN (SELECT COALESCE(SUM(amount_cents), 0) FROM acct_invoice_lines WHERE invoice_id = NEW.id) <> NEW.total_cents
  OR (SELECT COUNT(*) FROM acct_invoice_lines WHERE invoice_id = NEW.id) = 0
BEGIN SELECT RAISE(ABORT, 'an invoice''s total must equal its lines, and it needs at least one'); END;

CREATE TRIGGER acct_invoice_lines_before_their_invoice BEFORE INSERT ON acct_invoice_lines
WHEN EXISTS (SELECT 1 FROM acct_invoices WHERE id = NEW.invoice_id)
BEGIN SELECT RAISE(ABORT, 'an invoice''s lines are written before the invoice itself, and never added to after'); END;

CREATE TRIGGER acct_bills_total_matches_lines BEFORE INSERT ON acct_bills
WHEN (SELECT COALESCE(SUM(amount_cents), 0) FROM acct_bill_lines WHERE bill_id = NEW.id) <> NEW.total_cents
  OR (SELECT COUNT(*) FROM acct_bill_lines WHERE bill_id = NEW.id) = 0
BEGIN SELECT RAISE(ABORT, 'a bill''s total must equal its lines, and it needs at least one'); END;

CREATE TRIGGER acct_bill_lines_before_their_bill BEFORE INSERT ON acct_bill_lines
WHEN EXISTS (SELECT 1 FROM acct_bills WHERE id = NEW.bill_id)
BEGIN SELECT RAISE(ABORT, 'a bill''s lines are written before the bill itself, and never added to after'); END;

-- A sale lands in an income account and a purchase in an expense or asset account
-- (a till float, stock, a machine), and the account must be one still in use.
CREATE TRIGGER acct_invoice_lines_income_account BEFORE INSERT ON acct_invoice_lines
WHEN NOT EXISTS (SELECT 1 FROM acct_accounts
                  WHERE id = NEW.account_id AND is_active = 1 AND account_type = 'income')
BEGIN SELECT RAISE(ABORT, 'an invoice line posts to an active income account'); END;

CREATE TRIGGER acct_bill_lines_expense_account BEFORE INSERT ON acct_bill_lines
WHEN NOT EXISTS (SELECT 1 FROM acct_accounts
                  WHERE id = NEW.account_id AND is_active = 1 AND account_type IN ('expense', 'asset'))
BEGIN SELECT RAISE(ABORT, 'a bill line posts to an active expense or asset account'); END;

-- A bill's own lines can't be the payable (or the receivable) account: the bill row
-- itself credits payables, and a line that touched either control account would make
-- the aging reports disagree with the ledger. Invoice lines need no such rule, since
-- both control accounts are outside the one account type their lines allow.
CREATE TRIGGER acct_bill_lines_not_a_control_account BEFORE INSERT ON acct_bill_lines
WHEN EXISTS (SELECT 1 FROM acct_accounts WHERE id = NEW.account_id AND role IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'a bill line can''t post to the receivable or payable account'); END;

-- ---------------------------------------------------------------- a document matches its journal entries

-- Sending an invoice debits receivables by its total; the rest of the entry is its lines,
-- and 0001 already guarantees the entry balances.
CREATE TRIGGER acct_invoices_entry_matches BEFORE UPDATE OF entry_id ON acct_invoices
WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM acct_journal_entries e WHERE e.id = NEW.entry_id
           AND e.source = 'invoice' AND e.total_cents = NEW.total_cents
           AND (SELECT COALESCE(SUM(l.debit_cents), 0) FROM acct_journal_lines l
                  JOIN acct_accounts a ON a.id = l.account_id
                 WHERE l.entry_id = e.id AND a.role = 'accounts_receivable') = NEW.total_cents)
BEGIN SELECT RAISE(ABORT, 'an invoice''s entry must debit receivables by its total'); END;

-- Entering a bill credits payables by its total.
CREATE TRIGGER acct_bills_entry_matches BEFORE UPDATE OF entry_id ON acct_bills
WHEN NEW.entry_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM acct_journal_entries e WHERE e.id = NEW.entry_id
           AND e.source = 'bill' AND e.total_cents = NEW.total_cents
           AND (SELECT COALESCE(SUM(l.credit_cents), 0) FROM acct_journal_lines l
                  JOIN acct_accounts a ON a.id = l.account_id
                 WHERE l.entry_id = e.id AND a.role = 'accounts_payable') = NEW.total_cents)
BEGIN SELECT RAISE(ABORT, 'a bill''s entry must credit payables by its total'); END;

-- Voiding writes a reversal of the document's own entry (0001 checks that a reversal
-- mirrors it line for line), so the two together leave every account where it was.
CREATE TRIGGER acct_invoices_void_entry_reverses BEFORE UPDATE OF void_entry_id ON acct_invoices
WHEN NEW.void_entry_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM acct_journal_entries WHERE id = NEW.void_entry_id AND reverses_id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'a void must reverse the invoice''s own entry'); END;

CREATE TRIGGER acct_bills_void_entry_reverses BEFORE UPDATE OF void_entry_id ON acct_bills
WHEN NEW.void_entry_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM acct_journal_entries WHERE id = NEW.void_entry_id AND reverses_id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'a void must reverse the bill''s own entry'); END;

-- A payment's entry moves exactly its amount between the receivable (or payable)
-- account and the money account, the right way round.
CREATE TRIGGER acct_invoice_payments_entry_matches BEFORE INSERT ON acct_invoice_payments
WHEN NOT EXISTS (
        SELECT 1 FROM acct_journal_entries e WHERE e.id = NEW.entry_id
           AND e.source = 'invoice_payment' AND e.total_cents = NEW.amount_cents
           AND (SELECT COALESCE(SUM(l.credit_cents), 0) FROM acct_journal_lines l
                  JOIN acct_accounts a ON a.id = l.account_id
                 WHERE l.entry_id = e.id AND a.role = 'accounts_receivable') = NEW.amount_cents
           AND (SELECT COALESCE(SUM(l.debit_cents), 0) FROM acct_journal_lines l
                 WHERE l.entry_id = e.id AND l.account_id = NEW.deposit_account_id) = NEW.amount_cents)
BEGIN SELECT RAISE(ABORT, 'a receipt''s entry must credit receivables and debit the deposit account by its amount'); END;

CREATE TRIGGER acct_bill_payments_entry_matches BEFORE INSERT ON acct_bill_payments
WHEN NOT EXISTS (
        SELECT 1 FROM acct_journal_entries e WHERE e.id = NEW.entry_id
           AND e.source = 'bill_payment' AND e.total_cents = NEW.amount_cents
           AND (SELECT COALESCE(SUM(l.debit_cents), 0) FROM acct_journal_lines l
                  JOIN acct_accounts a ON a.id = l.account_id
                 WHERE l.entry_id = e.id AND a.role = 'accounts_payable') = NEW.amount_cents
           AND (SELECT COALESCE(SUM(l.credit_cents), 0) FROM acct_journal_lines l
                 WHERE l.entry_id = e.id AND l.account_id = NEW.paid_from_account_id) = NEW.amount_cents)
BEGIN SELECT RAISE(ABORT, 'a payment''s entry must debit payables and credit the account it came from by its amount'); END;

-- Money arrives in, and leaves from, an asset account that is still in use.
CREATE TRIGGER acct_invoice_payments_deposit_account BEFORE INSERT ON acct_invoice_payments
WHEN NOT EXISTS (SELECT 1 FROM acct_accounts WHERE id = NEW.deposit_account_id AND is_active = 1
                                               AND account_type = 'asset' AND role IS NULL)
BEGIN SELECT RAISE(ABORT, 'a receipt is deposited into an active asset account that isn''t receivables'); END;

CREATE TRIGGER acct_bill_payments_source_account BEFORE INSERT ON acct_bill_payments
WHEN NOT EXISTS (SELECT 1 FROM acct_accounts WHERE id = NEW.paid_from_account_id AND is_active = 1
                                               AND account_type = 'asset' AND role IS NULL)
BEGIN SELECT RAISE(ABORT, 'a payment is made from an active asset account that isn''t receivables'); END;

-- ---------------------------------------------------------------- statuses follow the money

-- Only money owed can be paid: not a draft, not a void, not one already settled.
CREATE TRIGGER acct_invoice_payments_invoice_is_sent BEFORE INSERT ON acct_invoice_payments
WHEN NOT EXISTS (SELECT 1 FROM acct_invoices WHERE id = NEW.invoice_id AND status = 'sent')
BEGIN SELECT RAISE(ABORT, 'a receipt is recorded against a sent invoice'); END;

CREATE TRIGGER acct_bill_payments_bill_is_sent BEFORE INSERT ON acct_bill_payments
WHEN NOT EXISTS (SELECT 1 FROM acct_bills WHERE id = NEW.bill_id AND status = 'sent')
BEGIN SELECT RAISE(ABORT, 'a payment is recorded against a received bill'); END;

-- An invoice is raised as a draft, and from there goes to sent (or void), then
-- paid (or void). Nothing comes back.
CREATE TRIGGER acct_invoices_born_draft BEFORE INSERT ON acct_invoices
WHEN NEW.status <> 'draft' OR NEW.amount_paid_cents <> 0
BEGIN SELECT RAISE(ABORT, 'an invoice is raised as an unpaid draft'); END;

CREATE TRIGGER acct_invoices_status_moves_forward BEFORE UPDATE OF status ON acct_invoices
WHEN NOT (OLD.status = 'draft' AND NEW.status IN ('sent', 'void')
       OR OLD.status = 'sent' AND NEW.status IN ('paid', 'void')
       OR OLD.status = NEW.status)
BEGIN SELECT RAISE(ABORT, 'an invoice goes draft -> sent -> paid, or to void, and never back'); END;

CREATE TRIGGER acct_bills_born_draft BEFORE INSERT ON acct_bills
WHEN NEW.status <> 'draft' OR NEW.amount_paid_cents <> 0
BEGIN SELECT RAISE(ABORT, 'a bill is entered as an unpaid draft'); END;

CREATE TRIGGER acct_bills_status_moves_forward BEFORE UPDATE OF status ON acct_bills
WHEN NOT (OLD.status = 'draft' AND NEW.status IN ('sent', 'void')
       OR OLD.status = 'sent' AND NEW.status IN ('paid', 'void')
       OR OLD.status = NEW.status)
BEGIN SELECT RAISE(ABORT, 'a bill goes draft -> sent -> paid, or to void, and never back'); END;

-- Nothing is voided once money has moved against it: that would hide a receipt.
CREATE TRIGGER acct_invoices_void_unpaid BEFORE UPDATE OF status ON acct_invoices
WHEN NEW.status = 'void' AND OLD.amount_paid_cents <> 0
BEGIN SELECT RAISE(ABORT, 'an invoice with payments against it is not voided'); END;

CREATE TRIGGER acct_bills_void_unpaid BEFORE UPDATE OF status ON acct_bills
WHEN NEW.status = 'void' AND OLD.amount_paid_cents <> 0
BEGIN SELECT RAISE(ABORT, 'a bill with payments against it is not voided'); END;

-- amount_paid_cents is exactly what the payment rows add up to, and it only grows.
-- Payments are written before the document is updated, so this sees them.
CREATE TRIGGER acct_invoices_paid_matches_payments BEFORE UPDATE OF amount_paid_cents ON acct_invoices
WHEN NEW.amount_paid_cents < OLD.amount_paid_cents
  OR NEW.amount_paid_cents <> (SELECT COALESCE(SUM(amount_cents), 0) FROM acct_invoice_payments
                               WHERE invoice_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'an invoice''s amount paid is the sum of its receipts, and never falls'); END;

CREATE TRIGGER acct_bills_paid_matches_payments BEFORE UPDATE OF amount_paid_cents ON acct_bills
WHEN NEW.amount_paid_cents < OLD.amount_paid_cents
  OR NEW.amount_paid_cents <> (SELECT COALESCE(SUM(amount_cents), 0) FROM acct_bill_payments
                                 WHERE bill_id = NEW.id)
BEGIN SELECT RAISE(ABORT, 'a bill''s amount paid is the sum of its payments, and never falls'); END;
