CREATE TABLE hr.shifts(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,staff_id BIGINT NOT NULL REFERENCES hr.staff(id),
 location_entity_key TEXT NOT NULL,location_record_id BIGINT NOT NULL REFERENCES core.entities(id),
 work_date DATE NOT NULL CHECK(work_date BETWEEN DATE '1900-01-01' AND DATE '9999-12-31'),
 start_time TIME NOT NULL,end_time TIME NOT NULL CHECK(end_time>start_time),actor_id BIGINT NOT NULL CHECK(actor_id>0),
 request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE hr.shift_events(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,shift_id BIGINT NOT NULL REFERENCES hr.shifts(id),
 event TEXT NOT NULL CHECK(event IN('scheduled','cancelled')),reason TEXT,actor_id BIGINT NOT NULL CHECK(actor_id>0),
 request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,UNIQUE(shift_id,event));
CREATE VIEW hr.shift_state AS SELECT s.*,COALESCE(e.event,'new') AS status FROM hr.shifts s
 LEFT JOIN LATERAL(SELECT event FROM hr.shift_events WHERE shift_id=s.id ORDER BY id DESC LIMIT 1)e ON true;
CREATE TRIGGER hr_shifts_immutable BEFORE UPDATE OR DELETE ON hr.shifts FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_shift_events_immutable BEFORE UPDATE OR DELETE ON hr.shift_events FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE FUNCTION hr.guard_shift() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected TEXT; business BIGINT; person hr.staff_state%ROWTYPE;
BEGIN
 PERFORM hr.lock_resources(); SELECT * INTO person FROM hr.staff_state WHERE id=NEW.staff_id;
 IF NOT FOUND OR person.status<>'active' OR NEW.work_date<person.employment_start_date THEN RAISE EXCEPTION 'shift requires active employment' USING ERRCODE='23514'; END IF;
 SELECT bindings_json::jsonb->'location'->>'entity' INTO expected FROM core.nerd_installations WHERE nerd_id='hr';
 SELECT substring(business_key FROM '^business:([0-9]+)$')::bigint INTO business FROM hr.database_identity WHERE singleton;
 IF NEW.location_entity_key<>expected OR NOT EXISTS(SELECT 1 FROM core.entities WHERE id=NEW.location_record_id AND entity_type=expected AND business_id=business) THEN RAISE EXCEPTION 'shift location is not an authoritative mapped location' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM hr.shift_state s WHERE s.staff_id=NEW.staff_id AND s.work_date=NEW.work_date AND s.status='scheduled' AND s.start_time<NEW.end_time AND s.end_time>NEW.start_time) THEN RAISE EXCEPTION 'staff shifts overlap' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM hr.leave_request_state q WHERE q.staff_id=NEW.staff_id AND q.status='approved' AND NEW.work_date BETWEEN q.start_date AND q.end_date
  AND (q.half IS NULL OR (q.half='am' AND NEW.start_time<TIME '12:00') OR (q.half='pm' AND NEW.end_time>TIME '12:00'))) THEN RAISE EXCEPTION 'shift overlaps approved leave' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_shift_guard BEFORE INSERT ON hr.shifts FOR EACH ROW EXECUTE FUNCTION hr.guard_shift();
CREATE FUNCTION hr.guard_shift_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current TEXT;
BEGIN PERFORM hr.lock_resources(); SELECT status INTO current FROM hr.shift_state WHERE id=NEW.shift_id;
 IF (NEW.event='scheduled' AND current<>'new') OR (NEW.event='cancelled' AND (current<>'scheduled' OR length(trim(COALESCE(NEW.reason,'')))=0)) THEN RAISE EXCEPTION 'invalid shift event transition' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER hr_shift_event_guard BEFORE INSERT ON hr.shift_events FOR EACH ROW EXECUTE FUNCTION hr.guard_shift_event();
CREATE FUNCTION hr.shift_needs_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM hr.shift_events WHERE shift_id=NEW.id AND event='scheduled') THEN RAISE EXCEPTION 'shift needs a scheduling event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE CONSTRAINT TRIGGER hr_shift_event_required AFTER INSERT ON hr.shifts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hr.shift_needs_event();
CREATE FUNCTION hr.guard_leave_shift_overlap() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE q hr.leave_requests%ROWTYPE;
BEGIN
 PERFORM hr.lock_resources();
 IF NEW.event='approve' THEN SELECT * INTO q FROM hr.leave_requests WHERE id=NEW.request_id;
  IF EXISTS(SELECT 1 FROM hr.shift_state s WHERE s.staff_id=q.staff_id AND s.status='scheduled' AND s.work_date BETWEEN q.start_date AND q.end_date
   AND (q.half IS NULL OR (q.half='am' AND s.start_time<TIME '12:00') OR (q.half='pm' AND s.end_time>TIME '12:00'))) THEN RAISE EXCEPTION 'cancel conflicting shifts before approving leave' USING ERRCODE='23514'; END IF;
 END IF; RETURN NEW;
END $$;
CREATE TRIGGER hr_leave_shift_guard BEFORE INSERT ON hr.leave_ledger FOR EACH ROW EXECUTE FUNCTION hr.guard_leave_shift_overlap();
CREATE FUNCTION hr.guard_termination_shifts() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 PERFORM hr.lock_resources();
 IF NEW.event='terminated' AND EXISTS(SELECT 1 FROM hr.shift_state s WHERE s.staff_id=NEW.staff_id AND s.status='scheduled' AND s.work_date>NEW.effective_date) THEN
  RAISE EXCEPTION 'cancel future shifts before termination' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER hr_termination_shift_guard BEFORE INSERT ON hr.employment_events FOR EACH ROW EXECUTE FUNCTION hr.guard_termination_shifts();

CREATE TABLE hr.onboarding_tasks(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,staff_id BIGINT NOT NULL REFERENCES hr.staff(id),
 title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 200),due_date DATE CHECK(due_date BETWEEN DATE '1900-01-01' AND DATE '9999-12-31'),
 actor_id BIGINT NOT NULL CHECK(actor_id>0),request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED);
CREATE TABLE hr.onboarding_events(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,task_id BIGINT NOT NULL REFERENCES hr.onboarding_tasks(id),
 event TEXT NOT NULL CHECK(event IN('added','completed','reopened')),actor_id BIGINT NOT NULL CHECK(actor_id>0),
 request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED);
CREATE VIEW hr.onboarding_state AS SELECT t.*,COALESCE(CASE WHEN e.event='completed' THEN 'completed' WHEN e.event IN('added','reopened') THEN 'pending' END,'new') AS status
 FROM hr.onboarding_tasks t LEFT JOIN LATERAL(SELECT event FROM hr.onboarding_events WHERE task_id=t.id ORDER BY id DESC LIMIT 1)e ON true;
CREATE TRIGGER hr_onboarding_immutable BEFORE UPDATE OR DELETE ON hr.onboarding_tasks FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_onboarding_history_immutable BEFORE UPDATE OR DELETE ON hr.onboarding_events FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE FUNCTION hr.guard_onboarding() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 PERFORM hr.lock_resources(); IF NOT EXISTS(SELECT 1 FROM hr.staff_state WHERE id=NEW.staff_id AND status='active' AND (NEW.due_date IS NULL OR NEW.due_date>=employment_start_date)) THEN RAISE EXCEPTION 'onboarding requires active employment and a valid due date' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER hr_onboarding_guard BEFORE INSERT ON hr.onboarding_tasks FOR EACH ROW EXECUTE FUNCTION hr.guard_onboarding();
CREATE FUNCTION hr.guard_onboarding_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current TEXT; employee BIGINT;
BEGIN PERFORM hr.lock_resources(); SELECT status,staff_id INTO current,employee FROM hr.onboarding_state WHERE id=NEW.task_id;
 IF NOT EXISTS(SELECT 1 FROM hr.staff_state WHERE id=employee AND status='active') OR
  (NEW.event='added' AND current<>'new') OR (NEW.event='completed' AND current<>'pending') OR (NEW.event='reopened' AND current<>'completed') THEN RAISE EXCEPTION 'invalid onboarding event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER hr_onboarding_event_guard BEFORE INSERT ON hr.onboarding_events FOR EACH ROW EXECUTE FUNCTION hr.guard_onboarding_event();
CREATE FUNCTION hr.onboarding_needs_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM hr.onboarding_events WHERE task_id=NEW.id AND event='added') THEN RAISE EXCEPTION 'onboarding requires an add event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE CONSTRAINT TRIGGER hr_onboarding_event_required AFTER INSERT ON hr.onboarding_tasks DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hr.onboarding_needs_event();

DO $$ DECLARE table_name TEXT; BEGIN
 FOREACH table_name IN ARRAY ARRAY['database_identity','schema_migrations','command_requests','staff','employment_events','leave_types','leave_policy_changes','leave_requests','leave_ledger','shifts','shift_events','onboarding_tasks','onboarding_events'] LOOP
  EXECUTE format('CREATE TRIGGER hr_no_truncate BEFORE TRUNCATE ON hr.%I FOR EACH STATEMENT EXECUTE FUNCTION hr.reject_mutation()',table_name);
 END LOOP;
END $$;
