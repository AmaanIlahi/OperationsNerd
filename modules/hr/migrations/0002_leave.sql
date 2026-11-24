-- HR module, migration 0002: leave types, policy and the leave ledger.
--
-- Amounts are whole half-days (an integer column named half_days), so a
-- balance is exact: no floating point. The commands take and return days.
--
-- A balance is a ledger: balance = SUM(hr_leave_ledger.half_days) for the
-- employee and leave type. Accruals and adjustments add or remove, an approval
-- debits the request, and cancelling an approved request credits it back.
-- Requesting, rejecting and cancelling a pending request write a zero entry, so
-- every step of a request is in the ledger. Pending requests are held against
-- the balance: available = balance - pending. A request's status is moved by
-- the trigger on its ledger entry, never by a caller.

INSERT INTO hr_command_names (name) VALUES
    ('create_leave_type'), ('set_leave_allowance'), ('accrue_annual_leave'), ('adjust_leave'),
    ('request_leave'), ('approve_leave'), ('reject_leave'), ('cancel_leave');

CREATE TABLE hr_leave_types (
    id                         INTEGER PRIMARY KEY,
    code                       TEXT    NOT NULL CHECK (code <> '' AND code = lower(code) AND code NOT GLOB '*[^a-z0-9_]*'),
    name                       TEXT    NOT NULL CHECK (name <> ''),
    paid                       INTEGER NOT NULL CHECK (paid IN (0, 1)),
    tracks_balance             INTEGER NOT NULL CHECK (tracks_balance IN (0, 1)),
    annual_allowance_half_days INTEGER NOT NULL CHECK (annual_allowance_half_days >= 0),
    idempotency_key            TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at                 TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (code),
    UNIQUE (idempotency_key),
    CHECK (tracks_balance = 1 OR annual_allowance_half_days = 0)
);

-- Only the allowance changes, and only to what the latest policy change says.
CREATE TRIGGER hr_leave_types_allowance_only BEFORE UPDATE ON hr_leave_types
WHEN NEW.id IS NOT OLD.id OR NEW.code IS NOT OLD.code OR NEW.name IS NOT OLD.name OR NEW.paid IS NOT OLD.paid
  OR NEW.tracks_balance IS NOT OLD.tracks_balance OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.annual_allowance_half_days IS NOT (SELECT annual_allowance_half_days FROM hr_leave_policy_changes
                                            WHERE leave_type_id = OLD.id ORDER BY id DESC LIMIT 1)
BEGIN SELECT RAISE(ABORT, 'a leave type changes only its allowance, through a policy change'); END;

CREATE TRIGGER hr_leave_types_no_delete BEFORE DELETE ON hr_leave_types
BEGIN SELECT RAISE(ABORT, 'hr_leave_types rows are kept'); END;

-- Allowance history. Past accruals keep the amount they were given; a change
-- only affects accruals made after it.
CREATE TABLE hr_leave_policy_changes (
    id                         INTEGER PRIMARY KEY,
    leave_type_id              INTEGER NOT NULL REFERENCES hr_leave_types (id),
    annual_allowance_half_days INTEGER NOT NULL CHECK (annual_allowance_half_days >= 0),
    reason                     TEXT    NOT NULL CHECK (reason <> ''),
    idempotency_key            TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at                 TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

CREATE TRIGGER hr_leave_policy_changes_no_update BEFORE UPDATE ON hr_leave_policy_changes
BEGIN SELECT RAISE(ABORT, 'hr_leave_policy_changes rows are immutable'); END;

CREATE TRIGGER hr_leave_policy_changes_no_delete BEFORE DELETE ON hr_leave_policy_changes
BEGIN SELECT RAISE(ABORT, 'hr_leave_policy_changes rows are immutable'); END;

CREATE TRIGGER hr_leave_policy_changes_tracked_only BEFORE INSERT ON hr_leave_policy_changes
WHEN (SELECT tracks_balance FROM hr_leave_types WHERE id = NEW.leave_type_id) = 0
BEGIN SELECT RAISE(ABORT, 'leave type has no balance, so no allowance'); END;

CREATE TRIGGER hr_leave_policy_changes_apply AFTER INSERT ON hr_leave_policy_changes
BEGIN
    UPDATE hr_leave_types SET annual_allowance_half_days = NEW.annual_allowance_half_days
     WHERE id = NEW.leave_type_id;
END;

CREATE TABLE hr_leave_requests (
    id              INTEGER PRIMARY KEY,
    employee_id     INTEGER NOT NULL REFERENCES hr_employees (id),
    leave_type_id   INTEGER NOT NULL REFERENCES hr_leave_types (id),
    start_date      TEXT    NOT NULL CHECK (start_date IS date(start_date) AND substr(start_date, 9, 2) <= substr(date(substr(start_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    end_date        TEXT    NOT NULL CHECK (end_date IS date(end_date) AND substr(end_date, 9, 2) <= substr(date(substr(end_date, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),
    half_days       INTEGER NOT NULL CHECK (half_days > 0),
    note            TEXT    CHECK (note <> ''),
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled')),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key),
    CHECK (end_date >= start_date),
    -- A request can't ask for more half-days than the calendar days it spans.
    CHECK (half_days <= 2 * (julianday(end_date) - julianday(start_date) + 1))
);

CREATE INDEX hr_leave_requests_employee ON hr_leave_requests (employee_id, status, start_date);

-- A request starts pending, for an employee who is currently employed, on or after their start date.
CREATE TRIGGER hr_leave_requests_born_pending BEFORE INSERT ON hr_leave_requests
WHEN NEW.status <> 'pending'
BEGIN SELECT RAISE(ABORT, 'a leave request starts pending'); END;

CREATE TRIGGER hr_leave_requests_employee_active BEFORE INSERT ON hr_leave_requests
WHEN NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.employee_id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'employee is not active'); END;

CREATE TRIGGER hr_leave_requests_after_start BEFORE INSERT ON hr_leave_requests
WHEN NEW.start_date < (SELECT start_date FROM hr_employees WHERE id = NEW.employee_id)
BEGIN SELECT RAISE(ABORT, 'leave starts before the employee''s start date'); END;

-- One employee can't be on two pending or approved leaves on the same day, of any type.
CREATE TRIGGER hr_leave_requests_no_overlap BEFORE INSERT ON hr_leave_requests
WHEN EXISTS (SELECT 1 FROM hr_leave_requests
              WHERE employee_id = NEW.employee_id AND status IN ('pending', 'approved')
                AND start_date <= NEW.end_date AND end_date >= NEW.start_date)
BEGIN SELECT RAISE(ABORT, 'leave request overlaps another pending or approved request'); END;

-- For a type with a balance, pending requests together never exceed the balance.
CREATE TRIGGER hr_leave_requests_within_available BEFORE INSERT ON hr_leave_requests
WHEN (SELECT tracks_balance FROM hr_leave_types WHERE id = NEW.leave_type_id) = 1
 AND NEW.half_days
     + (SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_requests
         WHERE employee_id = NEW.employee_id AND leave_type_id = NEW.leave_type_id AND status = 'pending')
     > (SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_ledger
         WHERE employee_id = NEW.employee_id AND leave_type_id = NEW.leave_type_id)
BEGIN SELECT RAISE(ABORT, 'leave request exceeds available balance'); END;

-- Status moves pending -> approved | rejected | cancelled, or approved -> cancelled, and
-- always to what the request's latest ledger entry says.
CREATE TRIGGER hr_leave_requests_status_flow BEFORE UPDATE ON hr_leave_requests
WHEN NOT ((OLD.status = 'pending' AND NEW.status IN ('approved', 'rejected', 'cancelled'))
          OR (OLD.status = 'approved' AND NEW.status = 'cancelled'))
BEGIN SELECT RAISE(ABORT, 'leave request status can''t move that way'); END;

CREATE TRIGGER hr_leave_requests_status_matches_ledger BEFORE UPDATE ON hr_leave_requests
WHEN NEW.status IS NOT (SELECT CASE entry_type WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected'
                                               WHEN 'cancel' THEN 'cancelled' ELSE 'pending' END
                          FROM hr_leave_ledger WHERE request_id = OLD.id ORDER BY id DESC LIMIT 1)
BEGIN SELECT RAISE(ABORT, 'leave request status must match its ledger'); END;

CREATE TRIGGER hr_leave_requests_fixed_columns BEFORE UPDATE ON hr_leave_requests
WHEN NEW.id IS NOT OLD.id OR NEW.employee_id IS NOT OLD.employee_id OR NEW.leave_type_id IS NOT OLD.leave_type_id
  OR NEW.start_date IS NOT OLD.start_date OR NEW.end_date IS NOT OLD.end_date OR NEW.half_days IS NOT OLD.half_days
  OR NEW.note IS NOT OLD.note OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only a leave request''s status changes'); END;

CREATE TRIGGER hr_leave_requests_no_delete BEFORE DELETE ON hr_leave_requests
BEGIN SELECT RAISE(ABORT, 'hr_leave_requests rows are kept'); END;

CREATE TABLE hr_leave_ledger (
    id              INTEGER PRIMARY KEY,
    employee_id     INTEGER NOT NULL REFERENCES hr_employees (id),
    leave_type_id   INTEGER NOT NULL REFERENCES hr_leave_types (id),
    entry_type      TEXT    NOT NULL CHECK (entry_type IN ('accrual', 'adjustment', 'request', 'approve', 'reject',
                                                           'cancel')),
    half_days       INTEGER NOT NULL,
    request_id      INTEGER REFERENCES hr_leave_requests (id),
    accrual_year    INTEGER CHECK (accrual_year BETWEEN 1900 AND 9999),
    as_of           TEXT    CHECK (as_of IS date(as_of) AND substr(as_of, 9, 2) <= substr(date(substr(as_of, 1, 8) || '01', '+1 month', '-1 day'), 9, 2)),       -- the business date of the entry, when given
    actor           TEXT    CHECK (actor <> ''),                -- who decided, for approve / reject / cancel
    reason          TEXT    CHECK (reason <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key),
    CHECK ((entry_type = 'accrual') = (accrual_year IS NOT NULL)),
    CHECK ((entry_type IN ('accrual', 'adjustment')) = (request_id IS NULL)),
    CHECK (entry_type <> 'accrual' OR half_days >= 0),
    CHECK (entry_type <> 'adjustment' OR (half_days <> 0 AND reason IS NOT NULL)),
    CHECK (entry_type NOT IN ('request', 'reject') OR half_days = 0),
    CHECK (entry_type <> 'approve' OR half_days < 0),
    CHECK (entry_type <> 'cancel' OR half_days >= 0)
);

CREATE INDEX hr_leave_ledger_balance ON hr_leave_ledger (employee_id, leave_type_id);
-- One accrual per employee, type and year; each request step at most once.
CREATE UNIQUE INDEX hr_leave_ledger_one_accrual ON hr_leave_ledger (employee_id, leave_type_id, accrual_year)
    WHERE entry_type = 'accrual';
CREATE UNIQUE INDEX hr_leave_ledger_one_step ON hr_leave_ledger (request_id, entry_type) WHERE request_id IS NOT NULL;

CREATE TRIGGER hr_leave_ledger_no_update BEFORE UPDATE ON hr_leave_ledger
BEGIN SELECT RAISE(ABORT, 'hr_leave_ledger rows are immutable'); END;

CREATE TRIGGER hr_leave_ledger_no_delete BEFORE DELETE ON hr_leave_ledger
BEGIN SELECT RAISE(ABORT, 'hr_leave_ledger rows are immutable'); END;

-- Accruals and adjustments are for leave types that keep a balance.
CREATE TRIGGER hr_leave_ledger_tracked_only BEFORE INSERT ON hr_leave_ledger
WHEN NEW.entry_type IN ('accrual', 'adjustment')
 AND (SELECT tracks_balance FROM hr_leave_types WHERE id = NEW.leave_type_id) = 0
BEGIN SELECT RAISE(ABORT, 'leave type has no balance'); END;

-- Nothing new is granted, requested or approved for someone who is no longer employed.
CREATE TRIGGER hr_leave_ledger_employee_active BEFORE INSERT ON hr_leave_ledger
WHEN NEW.entry_type IN ('accrual', 'adjustment', 'request', 'approve')
 AND NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.employee_id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'employee is not active'); END;

-- A request's entries carry its employee and leave type, and follow its status:
-- 'request' first while pending, 'approve' or 'reject' while pending, 'cancel' while pending or approved.
CREATE TRIGGER hr_leave_ledger_matches_request BEFORE INSERT ON hr_leave_ledger
WHEN NEW.request_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM hr_leave_requests
              WHERE id = NEW.request_id AND (employee_id <> NEW.employee_id OR leave_type_id <> NEW.leave_type_id))
BEGIN SELECT RAISE(ABORT, 'ledger entry does not match its leave request'); END;

CREATE TRIGGER hr_leave_ledger_request_step BEFORE INSERT ON hr_leave_ledger
WHEN NEW.request_id IS NOT NULL
 AND NOT CASE NEW.entry_type
       WHEN 'request' THEN NOT EXISTS (SELECT 1 FROM hr_leave_ledger WHERE request_id = NEW.request_id)
       WHEN 'cancel' THEN EXISTS (SELECT 1 FROM hr_leave_requests WHERE id = NEW.request_id
                                                                   AND status IN ('pending', 'approved'))
                          AND EXISTS (SELECT 1 FROM hr_leave_ledger WHERE request_id = NEW.request_id)
       ELSE EXISTS (SELECT 1 FROM hr_leave_requests WHERE id = NEW.request_id AND status = 'pending')
            AND EXISTS (SELECT 1 FROM hr_leave_ledger WHERE request_id = NEW.request_id)
     END
BEGIN SELECT RAISE(ABORT, 'leave request is not in a state for this step'); END;

-- An approval debits exactly the request; a cancel credits it back only if it was approved.
CREATE TRIGGER hr_leave_ledger_request_amount BEFORE INSERT ON hr_leave_ledger
WHEN NEW.request_id IS NOT NULL
 AND NEW.half_days <> CASE
       WHEN NEW.entry_type = 'approve' THEN -(SELECT half_days FROM hr_leave_requests WHERE id = NEW.request_id)
       WHEN NEW.entry_type = 'cancel' AND (SELECT status FROM hr_leave_requests WHERE id = NEW.request_id) = 'approved'
            THEN (SELECT half_days FROM hr_leave_requests WHERE id = NEW.request_id)
       ELSE 0 END
BEGIN SELECT RAISE(ABORT, 'ledger amount does not match its leave request'); END;

-- A balance never goes below zero, whoever writes the row.
CREATE TRIGGER hr_leave_ledger_balance_not_negative BEFORE INSERT ON hr_leave_ledger
WHEN NEW.half_days < 0
 AND (SELECT tracks_balance FROM hr_leave_types WHERE id = NEW.leave_type_id) = 1
 AND NEW.half_days + (SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_ledger
                       WHERE employee_id = NEW.employee_id AND leave_type_id = NEW.leave_type_id) < 0
BEGIN SELECT RAISE(ABORT, 'leave balance would go negative'); END;

-- A negative adjustment can't eat into what pending requests are holding.
CREATE TRIGGER hr_leave_ledger_adjustment_within_available BEFORE INSERT ON hr_leave_ledger
WHEN NEW.entry_type = 'adjustment' AND NEW.half_days < 0
 AND NEW.half_days
     + (SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_ledger
         WHERE employee_id = NEW.employee_id AND leave_type_id = NEW.leave_type_id)
     - (SELECT COALESCE(SUM(half_days), 0) FROM hr_leave_requests
         WHERE employee_id = NEW.employee_id AND leave_type_id = NEW.leave_type_id AND status = 'pending') < 0
BEGIN SELECT RAISE(ABORT, 'adjustment exceeds available balance'); END;

-- Each request step moves the request's status.
CREATE TRIGGER hr_leave_ledger_move_request AFTER INSERT ON hr_leave_ledger
WHEN NEW.entry_type IN ('approve', 'reject', 'cancel')
BEGIN
    UPDATE hr_leave_requests
       SET status = CASE NEW.entry_type WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected'
                                        ELSE 'cancelled' END
     WHERE id = NEW.request_id;
END;
