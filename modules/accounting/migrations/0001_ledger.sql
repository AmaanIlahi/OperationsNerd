-- Accounting module, migration 0001: the chart of accounts and the double-entry journal.
-- Each business has its own private database, so no table carries a business id.
-- Foreign keys need PRAGMA foreign_keys = ON on each connection; accounting.connect() sets it.
--
-- The journal is append-only. An entry and its lines are written once and never
-- change: a mistake is corrected by a reversing entry, not by an edit. Because
-- SQLite can't check a rule at COMMIT, the lines of an entry are inserted
-- first and the entry row last, so the row that closes an entry can be checked
-- against the lines that are already there: debits equal credits, both equal
-- the entry's total, and there are at least two lines. A line inserted after
-- its entry row exists is refused, so no entry can be unbalanced later.
--
-- Money is whole cents in INTEGER columns. No money is ever a float, so no sum
-- ever drifts. Amounts are capped (MAX_CENTS in accounting.py, and the CHECKs
-- below) far above any small business's numbers, so sums stay inside SQLite's
-- 64-bit integers.
--
-- Every date column is checked with `x IS date(x)` plus a day-of-month guard:
-- `substr(x, 9, 2) <= substr(date(substr(x, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)`.
-- `date()` alone accepts impossible days (2026-02-30, 2026-04-31, 2025-02-29) on
-- SQLite before 3.45; the guard compares the day with the last day of that month,
-- so the rule is the same on every version. This mirrors modules/hr.

-- Command names known to the idempotency log. Each migration adds its own, so
-- a later migration never has to rebuild acct_commands to widen a CHECK.
CREATE TABLE acct_command_names (
    name TEXT PRIMARY KEY CHECK (name <> '')
);

INSERT INTO acct_command_names (name) VALUES
    ('add_account'), ('update_account'), ('post_journal_entry'), ('reverse_journal_entry');

-- What caused an entry. Each migration adds its own, for the same reason as the
-- command names. 'payroll' is here from the start as the documented hook for the
-- HR module: when payroll exists it posts its entries with source = 'payroll'
-- (see README, "Hook for HR payroll"). Nothing in this module writes it.
CREATE TABLE acct_entry_sources (
    name TEXT PRIMARY KEY CHECK (name <> '')
);

INSERT INTO acct_entry_sources (name) VALUES ('manual'), ('payroll');

-- One row per command that succeeded. A key is unique across all commands
-- (the database is one business) and a replay returns the original result.
CREATE TABLE acct_commands (
    id              INTEGER PRIMARY KEY,
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    command         TEXT    NOT NULL REFERENCES acct_command_names (name),
    payload         TEXT    NOT NULL,
    result          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

CREATE TRIGGER acct_commands_no_update BEFORE UPDATE ON acct_commands
BEGIN SELECT RAISE(ABORT, 'acct_commands rows are immutable'); END;

CREATE TRIGGER acct_commands_no_delete BEFORE DELETE ON acct_commands
BEGIN SELECT RAISE(ABORT, 'acct_commands rows are immutable'); END;

-- ---------------------------------------------------------------- chart of accounts

-- role marks the one account each posting rule needs by name rather than by code,
-- so a business can number its accounts however it likes. 0002 uses them for
-- invoices and bills.
CREATE TABLE acct_accounts (
    id              INTEGER PRIMARY KEY,
    code            TEXT    NOT NULL CHECK (code <> ''),           -- the business's own number, e.g. '1100'
    name            TEXT    NOT NULL CHECK (name <> ''),
    account_type    TEXT    NOT NULL CHECK (account_type IN ('asset', 'liability', 'equity', 'income', 'expense')),
    role            TEXT    CHECK (role IN ('accounts_receivable', 'accounts_payable')),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    notes           TEXT    CHECK (notes <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (code),
    UNIQUE (idempotency_key),
    -- What customers owe is an asset; what the business owes suppliers is a liability.
    CHECK (role IS NULL
           OR (role = 'accounts_receivable' AND account_type = 'asset')
           OR (role = 'accounts_payable' AND account_type = 'liability'))
);

CREATE UNIQUE INDEX acct_accounts_name ON acct_accounts (lower(name));
CREATE UNIQUE INDEX acct_accounts_role ON acct_accounts (role) WHERE role IS NOT NULL;

-- Only the name, notes and is_active ever change: the ledger behind a code must keep meaning the same thing.
CREATE TRIGGER acct_accounts_fixed_columns BEFORE UPDATE ON acct_accounts
WHEN NEW.id IS NOT OLD.id OR NEW.code IS NOT OLD.code OR NEW.account_type IS NOT OLD.account_type
  OR NEW.role IS NOT OLD.role OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'an account''s code, type and role never change'); END;

CREATE TRIGGER acct_accounts_no_delete BEFORE DELETE ON acct_accounts
BEGIN SELECT RAISE(ABORT, 'accounts are kept: the journal refers to them'); END;

-- ---------------------------------------------------------------- the journal

CREATE TABLE acct_journal_entries (
    id              INTEGER PRIMARY KEY,
    entry_date      TEXT    NOT NULL CHECK (entry_date IS date(entry_date) AND substr(entry_date, 9, 2) <= substr(date(substr(entry_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    memo            TEXT    NOT NULL CHECK (memo <> ''),
    source          TEXT    NOT NULL REFERENCES acct_entry_sources (name),
    source_ref      TEXT    CHECK (source_ref <> ''),              -- the invoice number, pay run, ... that caused it
    total_cents     INTEGER NOT NULL CHECK (total_cents > 0 AND total_cents <= 10000000000000),
    reverses_id     INTEGER REFERENCES acct_journal_entries (id),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

-- An entry is reversed at most once; a second correction reverses the reversal.
CREATE UNIQUE INDEX acct_journal_entries_reverses ON acct_journal_entries (reverses_id) WHERE reverses_id IS NOT NULL;
CREATE INDEX acct_journal_entries_date ON acct_journal_entries (entry_date);
CREATE INDEX acct_journal_entries_source ON acct_journal_entries (source, source_ref);

CREATE TABLE acct_journal_lines (
    id           INTEGER PRIMARY KEY,
    -- The entry row is written after its lines (see the header), so this is checked at COMMIT.
    entry_id     INTEGER NOT NULL REFERENCES acct_journal_entries (id) DEFERRABLE INITIALLY DEFERRED,
    line_no      INTEGER NOT NULL CHECK (line_no >= 1),
    account_id   INTEGER NOT NULL REFERENCES acct_accounts (id),
    debit_cents  INTEGER NOT NULL DEFAULT 0 CHECK (debit_cents >= 0 AND debit_cents <= 10000000000000),
    credit_cents INTEGER NOT NULL DEFAULT 0 CHECK (credit_cents >= 0 AND credit_cents <= 10000000000000),
    memo         TEXT    CHECK (memo <> ''),
    UNIQUE (entry_id, line_no),
    -- A line is a debit or a credit, never both and never nothing.
    CHECK ((debit_cents = 0) <> (credit_cents = 0))
);

CREATE INDEX acct_journal_lines_account ON acct_journal_lines (account_id);

CREATE TRIGGER acct_journal_entries_no_update BEFORE UPDATE ON acct_journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable: post a reversing entry instead'); END;

CREATE TRIGGER acct_journal_entries_no_delete BEFORE DELETE ON acct_journal_entries
BEGIN SELECT RAISE(ABORT, 'journal entries are immutable: post a reversing entry instead'); END;

CREATE TRIGGER acct_journal_lines_no_update BEFORE UPDATE ON acct_journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable: post a reversing entry instead'); END;

CREATE TRIGGER acct_journal_lines_no_delete BEFORE DELETE ON acct_journal_lines
BEGIN SELECT RAISE(ABORT, 'journal lines are immutable: post a reversing entry instead'); END;

-- The whole double-entry rule. An entry's lines go in first and this fires when the
-- entry row closes them: nothing can be added to a finished entry (the trigger below),
-- so an entry that exists is balanced forever.
CREATE TRIGGER acct_journal_entries_balanced BEFORE INSERT ON acct_journal_entries
WHEN (SELECT COALESCE(SUM(debit_cents), 0) FROM acct_journal_lines WHERE entry_id = NEW.id) <> NEW.total_cents
  OR (SELECT COALESCE(SUM(credit_cents), 0) FROM acct_journal_lines WHERE entry_id = NEW.id) <> NEW.total_cents
BEGIN SELECT RAISE(ABORT, 'debits must equal credits, and both must equal the entry total'); END;

CREATE TRIGGER acct_journal_entries_two_lines BEFORE INSERT ON acct_journal_entries
WHEN (SELECT COUNT(*) FROM acct_journal_lines WHERE entry_id = NEW.id) < 2
BEGIN SELECT RAISE(ABORT, 'a journal entry needs at least two lines'); END;

CREATE TRIGGER acct_journal_lines_before_their_entry BEFORE INSERT ON acct_journal_lines
WHEN EXISTS (SELECT 1 FROM acct_journal_entries WHERE id = NEW.entry_id)
BEGIN SELECT RAISE(ABORT, 'an entry''s lines are written before the entry itself, and never added to after'); END;

CREATE TRIGGER acct_journal_lines_account_active BEFORE INSERT ON acct_journal_lines
WHEN NOT EXISTS (SELECT 1 FROM acct_accounts WHERE id = NEW.account_id AND is_active = 1)
BEGIN SELECT RAISE(ABORT, 'a journal line posts to an active account'); END;

-- A reversal is the same entry with its debits and credits swapped, line for line, so
-- the two together leave every account exactly where it was.
CREATE TRIGGER acct_journal_entries_reversal_mirrors BEFORE INSERT ON acct_journal_entries
WHEN NEW.reverses_id IS NOT NULL
 AND ((SELECT COUNT(*) FROM acct_journal_lines WHERE entry_id = NEW.reverses_id)
        <> (SELECT COUNT(*) FROM acct_journal_lines WHERE entry_id = NEW.id)
      OR EXISTS (SELECT 1 FROM acct_journal_lines o
                  WHERE o.entry_id = NEW.reverses_id
                    AND NOT EXISTS (SELECT 1 FROM acct_journal_lines r
                                     WHERE r.entry_id = NEW.id AND r.line_no = o.line_no
                                       AND r.account_id = o.account_id
                                       AND r.debit_cents = o.credit_cents
                                       AND r.credit_cents = o.debit_cents)))
BEGIN SELECT RAISE(ABORT, 'a reversal must mirror the entry it reverses, line for line'); END;
