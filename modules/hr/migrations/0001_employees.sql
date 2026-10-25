-- HR module, migration 0001: employee records.
-- Each business has its own private database, so no table carries a business id.
-- Foreign keys need PRAGMA foreign_keys = ON on each connection; hr.connect() sets it.
--
-- An employee's history is a ledger: hr_employment_events only appends. Each
-- event carries the full employee snapshot after the change, and a trigger
-- projects it onto hr_employees. hr_employees is never written by a caller:
-- its own triggers refuse any insert or update that doesn't match the event
-- for that version. So the current row and the history can't drift apart.
-- No pay, tax, bank or other sensitive personal fields (see README).

-- Command names known to the idempotency log. Each migration adds its own, so
-- a later migration never has to rebuild hr_commands to widen a CHECK.
CREATE TABLE hr_command_names (
    name TEXT PRIMARY KEY CHECK (name <> '')
);

INSERT INTO hr_command_names (name) VALUES ('add_employee'), ('update_employee'), ('terminate_employee');

-- One row per command that succeeded. A key is unique across all commands
-- (the database is one business) and a replay returns the original result.
CREATE TABLE hr_commands (
    id              INTEGER PRIMARY KEY,
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    command         TEXT    NOT NULL REFERENCES hr_command_names (name),
    payload         TEXT    NOT NULL,
    result          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (idempotency_key)
);

CREATE TRIGGER hr_commands_no_update BEFORE UPDATE ON hr_commands
BEGIN SELECT RAISE(ABORT, 'hr_commands rows are immutable'); END;

CREATE TRIGGER hr_commands_no_delete BEFORE DELETE ON hr_commands
BEGIN SELECT RAISE(ABORT, 'hr_commands rows are immutable'); END;

CREATE TABLE hr_employees (
    id                 INTEGER PRIMARY KEY,
    employee_number    TEXT    NOT NULL CHECK (employee_number <> ''),     -- the business's own code, e.g. 'E001'
    first_name         TEXT    NOT NULL CHECK (first_name <> ''),
    last_name          TEXT    CHECK (last_name <> ''),
    preferred_name     TEXT    CHECK (preferred_name <> ''),
    work_email         TEXT    CHECK (work_email LIKE '_%@_%'),
    work_phone         TEXT    CHECK (work_phone <> ''),
    job_title          TEXT    NOT NULL CHECK (job_title <> ''),
    department         TEXT    CHECK (department <> ''),
    manager_id         INTEGER REFERENCES hr_employees (id),
    employment_type    TEXT    NOT NULL CHECK (employment_type IN ('full_time', 'part_time', 'contractor',
                                                                   'temporary', 'intern')),
    work_location      TEXT    CHECK (work_location <> ''),
    start_date         TEXT    NOT NULL CHECK (start_date IS date(start_date)),
    status             TEXT    NOT NULL CHECK (status IN ('active', 'terminated')),
    end_date           TEXT    CHECK (end_date IS date(end_date)),
    termination_reason TEXT    CHECK (termination_reason <> ''),
    version            INTEGER NOT NULL CHECK (version >= 1),
    idempotency_key    TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (employee_number),
    UNIQUE (idempotency_key),
    CHECK ((status = 'terminated') = (end_date IS NOT NULL)),
    CHECK ((status = 'terminated') = (termination_reason IS NOT NULL)),
    CHECK (end_date IS NULL OR end_date >= start_date)
);

CREATE UNIQUE INDEX hr_employees_work_email ON hr_employees (lower(work_email)) WHERE work_email IS NOT NULL;
CREATE INDEX hr_employees_manager ON hr_employees (manager_id);

CREATE TABLE hr_employment_events (
    id              INTEGER PRIMARY KEY,
    employee_id     INTEGER NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    event           TEXT    NOT NULL CHECK (event IN ('hired', 'updated', 'terminated')),
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

CREATE TRIGGER hr_employment_events_no_update BEFORE UPDATE ON hr_employment_events
BEGIN SELECT RAISE(ABORT, 'hr_employment_events rows are immutable'); END;

CREATE TRIGGER hr_employment_events_no_delete BEFORE DELETE ON hr_employment_events
BEGIN SELECT RAISE(ABORT, 'hr_employment_events rows are immutable'); END;

-- Events come in order: the next version of the employee, starting at 1.
CREATE TRIGGER hr_employment_events_in_order BEFORE INSERT ON hr_employment_events
WHEN NEW.version <> COALESCE((SELECT version FROM hr_employees WHERE id = NEW.employee_id), 0) + 1
BEGIN SELECT RAISE(ABORT, 'employment events must follow the employee''s current version'); END;

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

-- A terminated employee is frozen: nothing about them changes afterwards.
CREATE TRIGGER hr_employees_terminated_frozen BEFORE UPDATE ON hr_employees
WHEN OLD.status = 'terminated'
BEGIN SELECT RAISE(ABORT, 'employee is terminated'); END;

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
