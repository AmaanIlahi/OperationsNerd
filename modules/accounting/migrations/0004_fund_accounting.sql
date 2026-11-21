-- Accounting module, migration 0004: fund accounting for non-profits.
--
-- Each journal line is tagged with a fund. Every journal entry must balance overall
-- (0001) and also within each fund: debits equal credits per fund_id. Every
-- business starts with a default General / unrestricted fund so existing books are
-- unchanged: migration backfills every line with that fund.

INSERT INTO acct_command_names (name) VALUES
    ('add_fund'), ('update_fund'), ('record_grant_receipt'), ('release_from_restriction');

INSERT INTO acct_entry_sources (name) VALUES
    ('grant_receipt'), ('fund_release');

CREATE TABLE acct_funds (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT    NOT NULL CHECK (code <> ''),
    name                  TEXT    NOT NULL CHECK (name <> ''),
    restriction_type      TEXT    NOT NULL CHECK (restriction_type IN (
        'unrestricted', 'temporarily_restricted', 'permanently_restricted')),
  -- Optional project or purpose the donor restricted the money to.
    purpose_description   TEXT    CHECK (purpose_description <> ''),
    grantor               TEXT    CHECK (grantor <> ''),
    start_date            TEXT    CHECK (start_date IS NULL
                                         OR (start_date IS date(start_date)
                                             AND substr(start_date, 9, 2) <= substr(
                                                 date(substr(start_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2))),
    end_date              TEXT    CHECK (end_date IS NULL
                                         OR (end_date IS date(end_date)
                                             AND substr(end_date, 9, 2) <= substr(
                                                 date(substr(end_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2))),
    is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    -- Exactly one fund per business is the default unrestricted fund (created here on upgrade).
    is_default            INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    idempotency_key       TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (code),
    UNIQUE (idempotency_key),
    CHECK (NOT is_default OR restriction_type = 'unrestricted')
);

CREATE UNIQUE INDEX acct_funds_name ON acct_funds (lower(name));
CREATE UNIQUE INDEX acct_funds_default ON acct_funds (is_default) WHERE is_default = 1;

CREATE TRIGGER acct_funds_fixed_columns BEFORE UPDATE ON acct_funds
WHEN NEW.id IS NOT OLD.id OR NEW.code IS NOT OLD.code
  OR NEW.restriction_type IS NOT OLD.restriction_type OR NEW.is_default IS NOT OLD.is_default
  OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a fund''s code, restriction type and default flag never change'); END;

CREATE TRIGGER acct_funds_no_delete BEFORE DELETE ON acct_funds
BEGIN SELECT RAISE(ABORT, 'funds are kept: the journal refers to them'); END;

CREATE TRIGGER acct_funds_default_active BEFORE UPDATE OF is_active ON acct_funds
WHEN NEW.is_default = 1 AND NEW.is_active = 0
BEGIN SELECT RAISE(ABORT, 'the default unrestricted fund stays active'); END;

-- The default unrestricted fund for every business. id 1 so journal_lines can default to it.
INSERT INTO acct_funds (id, code, name, restriction_type, is_active, is_default, idempotency_key)
VALUES (1, 'GEN', 'General', 'unrestricted', 1, 1, 'migration:0004:default_fund');

-- SQLite can't ADD a REFERENCES column with a non-NULL default; the FK is enforced by application
-- code and by new installs reading the same rule from this file's comment. DEFAULT 1 backfills lines.
ALTER TABLE acct_journal_lines ADD COLUMN fund_id INTEGER NOT NULL DEFAULT 1;

CREATE INDEX acct_journal_lines_fund ON acct_journal_lines (fund_id);

CREATE TRIGGER acct_journal_lines_fund_active BEFORE INSERT ON acct_journal_lines
WHEN NOT EXISTS (SELECT 1 FROM acct_funds WHERE id = NEW.fund_id AND is_active = 1)
BEGIN SELECT RAISE(ABORT, 'a journal line posts to an active fund'); END;

-- Each fund within an entry must balance on its own.
CREATE TRIGGER acct_journal_entries_balanced_per_fund BEFORE INSERT ON acct_journal_entries
WHEN EXISTS (
    SELECT fund_id FROM acct_journal_lines WHERE entry_id = NEW.id
    GROUP BY fund_id
    HAVING COALESCE(SUM(debit_cents), 0) <> COALESCE(SUM(credit_cents), 0))
BEGIN SELECT RAISE(ABORT, 'debits must equal credits within each fund'); END;

-- Reversal must mirror fund_id as well as account and amounts.
DROP TRIGGER acct_journal_entries_reversal_mirrors;
CREATE TRIGGER acct_journal_entries_reversal_mirrors BEFORE INSERT ON acct_journal_entries
WHEN NEW.reverses_id IS NOT NULL
 AND ((SELECT COUNT(*) FROM acct_journal_lines WHERE entry_id = NEW.reverses_id)
        <> (SELECT COUNT(*) FROM acct_journal_lines WHERE entry_id = NEW.id)
      OR EXISTS (SELECT 1 FROM acct_journal_lines o
                  WHERE o.entry_id = NEW.reverses_id
                    AND NOT EXISTS (SELECT 1 FROM acct_journal_lines r
                                     WHERE r.entry_id = NEW.id AND r.line_no = o.line_no
                                       AND r.account_id = o.account_id
                                       AND r.fund_id = o.fund_id
                                       AND r.debit_cents = o.credit_cents
                                       AND r.credit_cents = o.debit_cents)))
BEGIN SELECT RAISE(ABORT, 'a reversal must mirror the entry it reverses, line for line'); END;

-- Documents may pin spending to a restricted fund; NULL means the default unrestricted fund.
ALTER TABLE acct_invoices ADD COLUMN fund_id INTEGER REFERENCES acct_funds (id);
ALTER TABLE acct_bills ADD COLUMN fund_id INTEGER REFERENCES acct_funds (id);
