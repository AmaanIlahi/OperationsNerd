-- HR module, migration 0003: rehiring, employment events in date order, clean
-- identity fields, morning/afternoon half days, and no leave written for
-- someone who has left.
--
-- Released migrations never change, so the 0001/0002 rules this loosens or
-- tightens are replaced here. hr_employment_events is rebuilt, because SQLite
-- can't widen a CHECK in place (the new event is 'rehired'); its rows are
-- copied unchanged. Triggers that change are dropped and created again.

INSERT INTO hr_command_names (name) VALUES ('rehire_employee');

-- These name hr_employment_events, so they go while it is rebuilt and come back after.
DROP TRIGGER hr_employees_insert_matches_event;
DROP TRIGGER hr_employees_update_matches_event;
-- Replaced below: a terminated employee can come back, but only through a 'rehired' event.
DROP TRIGGER hr_employees_terminated_frozen;
-- Unchanged, but created again after the two above so that SQLite (which runs the newest trigger first)
-- checks a row in the same order as before this migration, and refuses it with the same message.
DROP TRIGGER hr_employees_born_active;
DROP TRIGGER hr_employees_version_step;
DROP TRIGGER hr_employees_fixed_columns;
DROP TRIGGER hr_employees_manager_active_on_insert;
DROP TRIGGER hr_employees_manager_active_on_update;
DROP TRIGGER hr_employees_no_orphaned_reports;
DROP TRIGGER hr_employees_no_delete;

CREATE TABLE hr_employment_events_v3 (
    id              INTEGER PRIMARY KEY,
    employee_id     INTEGER NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    event           TEXT    NOT NULL CHECK (event IN ('hired', 'updated', 'terminated', 'rehired')),
    effective_date  TEXT    NOT NULL CHECK (effective_date IS date(effective_date)),
    changes         TEXT    NOT NULL CHECK (json_valid(changes) AND json_type(changes) = 'object'),
    snapshot        TEXT    NOT NULL CHECK (json_valid(snapshot) AND json_type(snapshot) = 'object'),
    reason          TEXT    CHECK (reason <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (employee_id, version),
    UNIQUE (idempotency_key),
    -- The trigger below creates the employee row after this one, so the check waits for COMMIT.
    FOREIGN KEY (employee_id) REFERENCES hr_employees (id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((event = 'hired') = (version = 1)),
    CHECK ((event = 'terminated') = (json_extract(snapshot, '$.status') = 'terminated')),
    CHECK (event <> 'terminated' OR json_extract(snapshot, '$.end_date') = effective_date)
);

INSERT INTO hr_employment_events_v3 (id, employee_id, version, event, effective_date, changes, snapshot, reason,
                                     idempotency_key, created_at)
SELECT id, employee_id, version, event, effective_date, changes, snapshot, reason, idempotency_key, created_at
  FROM hr_employment_events ORDER BY id;

DROP TABLE hr_employment_events;

ALTER TABLE hr_employment_events_v3 RENAME TO hr_employment_events;

CREATE TRIGGER hr_employment_events_no_update BEFORE UPDATE ON hr_employment_events
BEGIN SELECT RAISE(ABORT, 'hr_employment_events rows are immutable'); END;

CREATE TRIGGER hr_employment_events_no_delete BEFORE DELETE ON hr_employment_events
BEGIN SELECT RAISE(ABORT, 'hr_employment_events rows are immutable'); END;

-- Events come in order: the next version of the employee, starting at 1.
CREATE TRIGGER hr_employment_events_in_order BEFORE INSERT ON hr_employment_events
WHEN NEW.version <> COALESCE((SELECT version FROM hr_employees WHERE id = NEW.employee_id), 0) + 1
BEGIN SELECT RAISE(ABORT, 'employment events must follow the employee''s current version'); END;

-- ... and in date order: no event is dated before the one before it (so none before the hire either).
CREATE TRIGGER hr_employment_events_dates_in_order BEFORE INSERT ON hr_employment_events
WHEN NEW.version > 1
 AND NEW.effective_date < (SELECT effective_date FROM hr_employment_events
                            WHERE employee_id = NEW.employee_id ORDER BY version DESC LIMIT 1)
BEGIN SELECT RAISE(ABORT, 'an employment event can''t be dated before the previous one'); END;

-- A rehire brings back someone who has left, after their last day.
CREATE TRIGGER hr_employment_events_rehire_after_end BEFORE INSERT ON hr_employment_events
WHEN NEW.event = 'rehired'
 AND NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.employee_id AND status = 'terminated'
                                             AND end_date < NEW.effective_date)
BEGIN SELECT RAISE(ABORT, 'a rehire follows a termination and starts after its end date'); END;

-- New employee numbers carry no surrounding whitespace, and a work email that is set is trimmed lowercase.
CREATE TRIGGER hr_employment_events_clean_identity BEFORE INSERT ON hr_employment_events
WHEN (NEW.event = 'hired'
      AND json_extract(NEW.snapshot, '$.employee_number')
          IS NOT trim(json_extract(NEW.snapshot, '$.employee_number'), ' ' || char(9, 10, 13)))
  OR (json_type(NEW.changes, '$.work_email') IS NOT NULL
      AND json_extract(NEW.snapshot, '$.work_email')
          IS NOT lower(trim(json_extract(NEW.snapshot, '$.work_email'), ' ' || char(9, 10, 13))))
BEGIN SELECT RAISE(ABORT, 'employee number and work email must be trimmed (and the email lowercase)'); END;

-- Projects each event onto hr_employees: the hire creates the row, later events replace it. A later event
-- copies the fixed columns too, so an event that claims a new number or start date is refused by
-- hr_employees_fixed_columns instead of leaving the event and the row disagreeing.
CREATE TRIGGER hr_employment_events_project_hire AFTER INSERT ON hr_employment_events
WHEN NEW.version = 1
BEGIN
    INSERT INTO hr_employees (id, employee_number, first_name, last_name, preferred_name, work_email, work_phone,
                              job_title, department, manager_id, employment_type, work_location, start_date,
                              status, end_date, termination_reason, version, idempotency_key)
    VALUES (NEW.employee_id,
            json_extract(NEW.snapshot, '$.employee_number'), json_extract(NEW.snapshot, '$.first_name'),
            json_extract(NEW.snapshot, '$.last_name'), json_extract(NEW.snapshot, '$.preferred_name'),
            json_extract(NEW.snapshot, '$.work_email'), json_extract(NEW.snapshot, '$.work_phone'),
            json_extract(NEW.snapshot, '$.job_title'), json_extract(NEW.snapshot, '$.department'),
            json_extract(NEW.snapshot, '$.manager_id'), json_extract(NEW.snapshot, '$.employment_type'),
            json_extract(NEW.snapshot, '$.work_location'), json_extract(NEW.snapshot, '$.start_date'),
            json_extract(NEW.snapshot, '$.status'), json_extract(NEW.snapshot, '$.end_date'),
            json_extract(NEW.snapshot, '$.termination_reason'), NEW.version, NEW.idempotency_key);
END;

CREATE TRIGGER hr_employment_events_project_change AFTER INSERT ON hr_employment_events
WHEN NEW.version > 1
BEGIN
    UPDATE hr_employees
       SET employee_number = json_extract(NEW.snapshot, '$.employee_number'),
           start_date = json_extract(NEW.snapshot, '$.start_date'),
           first_name = json_extract(NEW.snapshot, '$.first_name'),
           last_name = json_extract(NEW.snapshot, '$.last_name'),
           preferred_name = json_extract(NEW.snapshot, '$.preferred_name'),
           work_email = json_extract(NEW.snapshot, '$.work_email'),
           work_phone = json_extract(NEW.snapshot, '$.work_phone'),
           job_title = json_extract(NEW.snapshot, '$.job_title'),
           department = json_extract(NEW.snapshot, '$.department'),
           manager_id = json_extract(NEW.snapshot, '$.manager_id'),
           employment_type = json_extract(NEW.snapshot, '$.employment_type'),
           work_location = json_extract(NEW.snapshot, '$.work_location'),
           status = json_extract(NEW.snapshot, '$.status'),
           end_date = json_extract(NEW.snapshot, '$.end_date'),
           termination_reason = json_extract(NEW.snapshot, '$.termination_reason'),
           version = NEW.version
     WHERE id = NEW.employee_id;
END;

-- hr_employees only ever holds what its latest event says, whoever writes the row.
CREATE TRIGGER hr_employees_insert_matches_event BEFORE INSERT ON hr_employees
WHEN NOT EXISTS (
    SELECT 1 FROM hr_employment_events e
     WHERE e.employee_id = NEW.id AND e.version = NEW.version AND e.event = 'hired'
       AND e.idempotency_key = NEW.idempotency_key
       AND json_extract(e.snapshot, '$.employee_number') IS NEW.employee_number
       AND json_extract(e.snapshot, '$.first_name') IS NEW.first_name
       AND json_extract(e.snapshot, '$.last_name') IS NEW.last_name
       AND json_extract(e.snapshot, '$.preferred_name') IS NEW.preferred_name
       AND json_extract(e.snapshot, '$.work_email') IS NEW.work_email
       AND json_extract(e.snapshot, '$.work_phone') IS NEW.work_phone
       AND json_extract(e.snapshot, '$.job_title') IS NEW.job_title
       AND json_extract(e.snapshot, '$.department') IS NEW.department
       AND json_extract(e.snapshot, '$.manager_id') IS NEW.manager_id
       AND json_extract(e.snapshot, '$.employment_type') IS NEW.employment_type
       AND json_extract(e.snapshot, '$.work_location') IS NEW.work_location
       AND json_extract(e.snapshot, '$.start_date') IS NEW.start_date
       AND json_extract(e.snapshot, '$.status') IS NEW.status
       AND json_extract(e.snapshot, '$.end_date') IS NEW.end_date
       AND json_extract(e.snapshot, '$.termination_reason') IS NEW.termination_reason)
BEGIN SELECT RAISE(ABORT, 'employee rows are written only by an employment event'); END;

CREATE TRIGGER hr_employees_update_matches_event BEFORE UPDATE ON hr_employees
WHEN NOT EXISTS (
    SELECT 1 FROM hr_employment_events e
     WHERE e.employee_id = OLD.id AND e.version = NEW.version
       AND json_extract(e.snapshot, '$.employee_number') IS NEW.employee_number
       AND json_extract(e.snapshot, '$.start_date') IS NEW.start_date
       AND json_extract(e.snapshot, '$.first_name') IS NEW.first_name
       AND json_extract(e.snapshot, '$.last_name') IS NEW.last_name
       AND json_extract(e.snapshot, '$.preferred_name') IS NEW.preferred_name
       AND json_extract(e.snapshot, '$.work_email') IS NEW.work_email
       AND json_extract(e.snapshot, '$.work_phone') IS NEW.work_phone
       AND json_extract(e.snapshot, '$.job_title') IS NEW.job_title
       AND json_extract(e.snapshot, '$.department') IS NEW.department
       AND json_extract(e.snapshot, '$.manager_id') IS NEW.manager_id
       AND json_extract(e.snapshot, '$.employment_type') IS NEW.employment_type
       AND json_extract(e.snapshot, '$.work_location') IS NEW.work_location
       AND json_extract(e.snapshot, '$.status') IS NEW.status
       AND json_extract(e.snapshot, '$.end_date') IS NEW.end_date
       AND json_extract(e.snapshot, '$.termination_reason') IS NEW.termination_reason)
BEGIN SELECT RAISE(ABORT, 'employee rows are written only by an employment event'); END;

-- A new employee starts active, at version 1.
CREATE TRIGGER hr_employees_born_active BEFORE INSERT ON hr_employees
WHEN NEW.status <> 'active' OR NEW.version <> 1
BEGIN SELECT RAISE(ABORT, 'an employee starts active at version 1'); END;

-- Each change moves the version up by exactly one.
CREATE TRIGGER hr_employees_version_step BEFORE UPDATE ON hr_employees
WHEN NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'each change moves the employee version up by one'); END;

-- A terminated employee is frozen, except that a 'rehired' event brings them back.
CREATE TRIGGER hr_employees_terminated_frozen BEFORE UPDATE ON hr_employees
WHEN OLD.status = 'terminated'
 AND (SELECT event FROM hr_employment_events WHERE employee_id = OLD.id AND version = NEW.version) IS NOT 'rehired'
BEGIN SELECT RAISE(ABORT, 'employee is terminated'); END;

-- Coming back, they report to someone who is employed (hr_employees_manager_active_on_update only looks
-- at a manager that changes).
CREATE TRIGGER hr_employees_rehire_manager_active BEFORE UPDATE ON hr_employees
WHEN OLD.status = 'terminated' AND NEW.status = 'active' AND NEW.manager_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.manager_id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'manager must be an active employee'); END;

-- The id, employee number, start date, key and creation time never change.
CREATE TRIGGER hr_employees_fixed_columns BEFORE UPDATE ON hr_employees
WHEN NEW.id IS NOT OLD.id OR NEW.employee_number IS NOT OLD.employee_number
  OR NEW.start_date IS NOT OLD.start_date OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'employee id, number and start date never change'); END;

-- A manager is someone else who is currently employed.
CREATE TRIGGER hr_employees_manager_active_on_insert BEFORE INSERT ON hr_employees
WHEN NEW.manager_id IS NOT NULL
 AND NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.manager_id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'manager must be an active employee'); END;

CREATE TRIGGER hr_employees_manager_active_on_update BEFORE UPDATE OF manager_id ON hr_employees
WHEN NEW.manager_id IS NOT NULL AND NEW.manager_id IS NOT OLD.manager_id
 AND (NEW.manager_id = OLD.id
      OR NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.manager_id AND status = 'active'))
BEGIN SELECT RAISE(ABORT, 'manager must be an active employee'); END;

-- Nobody is left reporting to someone who has gone: reassign the reports first.
CREATE TRIGGER hr_employees_no_orphaned_reports BEFORE UPDATE OF status ON hr_employees
WHEN NEW.status = 'terminated'
 AND EXISTS (SELECT 1 FROM hr_employees WHERE manager_id = OLD.id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'employee still has active direct reports'); END;

CREATE TRIGGER hr_employees_no_delete BEFORE DELETE ON hr_employees
BEGIN SELECT RAISE(ABORT, 'hr_employees rows are kept'); END;

-- Half days: a morning ('am') or afternoon ('pm') half of one date. Requests from before this migration
-- have no half and keep blocking their whole day.
ALTER TABLE hr_leave_requests ADD COLUMN half TEXT CHECK (half IN ('am', 'pm'));

CREATE TRIGGER hr_leave_requests_half_shape BEFORE INSERT ON hr_leave_requests
WHEN NEW.half IS NOT NULL AND (NEW.half_days <> 1 OR NEW.start_date <> NEW.end_date)
BEGIN SELECT RAISE(ABORT, 'a morning or afternoon half is one half-day on one date'); END;

-- One employee can't be on two pending or approved leaves at the same time, of any type; the morning and
-- the afternoon of one date are different times.
DROP TRIGGER hr_leave_requests_no_overlap;
CREATE TRIGGER hr_leave_requests_no_overlap BEFORE INSERT ON hr_leave_requests
WHEN EXISTS (SELECT 1 FROM hr_leave_requests r
              WHERE r.employee_id = NEW.employee_id AND r.status IN ('pending', 'approved')
                AND r.start_date <= NEW.end_date AND r.end_date >= NEW.start_date
                AND NOT (r.half IS NOT NULL AND NEW.half IS NOT NULL AND r.half <> NEW.half))
BEGIN SELECT RAISE(ABORT, 'leave request overlaps another pending or approved request'); END;

-- Leave starts on or after the start of the employee's current employment (their latest rehire, or hire).
DROP TRIGGER hr_leave_requests_after_start;
CREATE TRIGGER hr_leave_requests_after_start BEFORE INSERT ON hr_leave_requests
WHEN NEW.start_date < COALESCE((SELECT MAX(effective_date) FROM hr_employment_events
                                 WHERE employee_id = NEW.employee_id AND event = 'rehired'),
                                (SELECT start_date FROM hr_employees WHERE id = NEW.employee_id))
BEGIN SELECT RAISE(ABORT, 'leave starts before the employee''s start date'); END;

DROP TRIGGER hr_leave_requests_fixed_columns;
CREATE TRIGGER hr_leave_requests_fixed_columns BEFORE UPDATE ON hr_leave_requests
WHEN NEW.id IS NOT OLD.id OR NEW.employee_id IS NOT OLD.employee_id OR NEW.leave_type_id IS NOT OLD.leave_type_id
  OR NEW.start_date IS NOT OLD.start_date OR NEW.end_date IS NOT OLD.end_date OR NEW.half_days IS NOT OLD.half_days
  OR NEW.note IS NOT OLD.note OR NEW.idempotency_key IS NOT OLD.idempotency_key OR NEW.created_at IS NOT OLD.created_at
  OR NEW.half IS NOT OLD.half
BEGIN SELECT RAISE(ABORT, 'only a leave request''s status changes'); END;

-- Nothing at all is written to the leave ledger of someone who is no longer employed: termination settles
-- their leave in the same transaction, before the 'terminated' event, so a later cancel can't credit them.
DROP TRIGGER hr_leave_ledger_employee_active;
CREATE TRIGGER hr_leave_ledger_employee_active BEFORE INSERT ON hr_leave_ledger
WHEN NOT EXISTS (SELECT 1 FROM hr_employees WHERE id = NEW.employee_id AND status = 'active')
BEGIN SELECT RAISE(ABORT, 'employee is not active'); END;
