CREATE TABLE hr.database_identity(singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton), business_key TEXT NOT NULL);
CREATE TABLE hr.command_requests(idempotency_key TEXT PRIMARY KEY,operation TEXT NOT NULL,payload JSONB NOT NULL,result JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE FUNCTION hr.lock_resources() RETURNS void LANGUAGE sql AS $$ SELECT pg_advisory_xact_lock(684728314520) $$;
CREATE FUNCTION hr.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'HR history is immutable' USING ERRCODE='23514'; END $$;
CREATE TRIGGER hr_identity_immutable BEFORE UPDATE OR DELETE ON hr.database_identity FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_migrations_immutable BEFORE UPDATE OR DELETE ON hr.schema_migrations FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_commands_immutable BEFORE UPDATE OR DELETE ON hr.command_requests FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();

CREATE TABLE hr.staff(
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, entity_key TEXT NOT NULL,
 record_id BIGINT NOT NULL REFERENCES core.entities(id), employee_number TEXT NOT NULL UNIQUE CHECK(length(trim(employee_number)) BETWEEN 1 AND 200),
 UNIQUE(entity_key,record_id));
CREATE TABLE hr.employment_events(
 id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, staff_id BIGINT NOT NULL REFERENCES hr.staff(id),
 version INTEGER NOT NULL CHECK(version>0), event TEXT NOT NULL CHECK(event IN('hired','updated','terminated','rehired')),
 effective_date DATE NOT NULL CHECK(effective_date BETWEEN DATE '1900-01-01' AND DATE '9999-12-31'),
 snapshot JSONB NOT NULL CHECK(jsonb_typeof(snapshot)='object'), reason TEXT,
 actor_id BIGINT NOT NULL CHECK(actor_id>0), request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(staff_id,version));
CREATE VIEW hr.staff_state AS SELECT s.*,e.version,e.event,e.effective_date,e.snapshot,
 (e.snapshot->>'status') AS status, (e.snapshot->>'start_date')::date AS start_date,
 (e.snapshot->>'employment_start_date')::date AS employment_start_date,
 NULLIF(e.snapshot->>'end_date','')::date AS end_date
 FROM hr.staff s JOIN LATERAL(SELECT * FROM hr.employment_events WHERE staff_id=s.id ORDER BY version DESC LIMIT 1)e ON true;
CREATE TRIGGER hr_staff_immutable BEFORE UPDATE OR DELETE ON hr.staff FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_employment_immutable BEFORE UPDATE OR DELETE ON hr.employment_events FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE FUNCTION hr.guard_staff() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE expected TEXT; business BIGINT;
BEGIN
 PERFORM hr.lock_resources();
 SELECT (bindings_json::jsonb->'employee'->>'entity') INTO expected FROM core.nerd_installations WHERE nerd_id='hr' AND status IN('enabled','enabling');
 SELECT substring(business_key FROM '^business:([0-9]+)$')::bigint INTO business FROM hr.database_identity WHERE singleton;
 IF expected IS NULL OR NEW.entity_key<>expected OR NOT EXISTS(SELECT 1 FROM core.entities WHERE id=NEW.record_id AND entity_type=expected AND business_id=business) THEN
  RAISE EXCEPTION 'staff must reference the authoritative mapped core employee' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_staff_guard BEFORE INSERT ON hr.staff FOR EACH ROW EXECUTE FUNCTION hr.guard_staff();
CREATE FUNCTION hr.staff_needs_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM hr.employment_events WHERE staff_id=NEW.id) THEN RAISE EXCEPTION 'staff requires an employment event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE CONSTRAINT TRIGGER hr_staff_event_required AFTER INSERT ON hr.staff DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hr.staff_needs_event();

CREATE TABLE hr.leave_types(code TEXT PRIMARY KEY CHECK(length(code) BETWEEN 1 AND 50),name TEXT NOT NULL,paid BOOLEAN NOT NULL,tracks_balance BOOLEAN NOT NULL);
CREATE TABLE hr.leave_policy_changes(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,leave_type TEXT NOT NULL REFERENCES hr.leave_types(code),
 annual_allowance_half_days INTEGER NOT NULL CHECK(annual_allowance_half_days BETWEEN 0 AND 200000),reason TEXT NOT NULL CHECK(length(trim(reason))>0),
 actor_id BIGINT NOT NULL CHECK(actor_id>0),request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE VIEW hr.leave_type_state AS SELECT t.*,COALESCE(p.annual_allowance_half_days,0) AS annual_allowance_half_days
 FROM hr.leave_types t LEFT JOIN LATERAL(SELECT annual_allowance_half_days FROM hr.leave_policy_changes WHERE leave_type=t.code ORDER BY id DESC LIMIT 1)p ON true;
CREATE TRIGGER hr_leave_types_immutable BEFORE UPDATE OR DELETE ON hr.leave_types FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_policy_immutable BEFORE UPDATE OR DELETE ON hr.leave_policy_changes FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE FUNCTION hr.guard_policy() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 PERFORM hr.lock_resources();
 IF NOT (SELECT tracks_balance FROM hr.leave_types WHERE code=NEW.leave_type) AND NEW.annual_allowance_half_days<>0 THEN RAISE EXCEPTION 'untracked leave has no allowance' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE TRIGGER hr_policy_guard BEFORE INSERT ON hr.leave_policy_changes FOR EACH ROW EXECUTE FUNCTION hr.guard_policy();
CREATE TABLE hr.leave_requests(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,staff_id BIGINT NOT NULL REFERENCES hr.staff(id),leave_type TEXT NOT NULL REFERENCES hr.leave_types(code),
 start_date DATE NOT NULL CHECK(start_date BETWEEN DATE '1900-01-01' AND DATE '9999-12-31'),end_date DATE NOT NULL CHECK(end_date BETWEEN DATE '1900-01-01' AND DATE '9999-12-31'),
 half TEXT CHECK(half IN('am','pm')),half_days INTEGER NOT NULL CHECK(half_days>0),note TEXT,
 replaces_request_id BIGINT REFERENCES hr.leave_requests(id),actor_id BIGINT NOT NULL CHECK(actor_id>0),
 request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,
 CHECK(end_date>=start_date AND end_date-start_date<=3660),CHECK(half IS NULL OR start_date=end_date));
CREATE TABLE hr.leave_ledger(id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,staff_id BIGINT NOT NULL REFERENCES hr.staff(id),leave_type TEXT NOT NULL REFERENCES hr.leave_types(code),
 event TEXT NOT NULL CHECK(event IN('grant','adjustment','request','approve','reject','cancel')),half_days INTEGER NOT NULL,
 request_id BIGINT REFERENCES hr.leave_requests(id),accrual_year INTEGER CHECK(accrual_year BETWEEN 1900 AND 9999),as_of DATE NOT NULL,
 reason TEXT,termination_date DATE,actor_id BIGINT NOT NULL CHECK(actor_id>0),request_key TEXT NOT NULL REFERENCES hr.command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,
 CHECK((event IN('grant','adjustment'))=(request_id IS NULL)),CHECK((event='grant')=(accrual_year IS NOT NULL)),CHECK(termination_date IS NULL OR event='cancel'));
CREATE UNIQUE INDEX hr_one_grant ON hr.leave_ledger(staff_id,leave_type,accrual_year) WHERE event='grant';
CREATE UNIQUE INDEX hr_one_leave_step ON hr.leave_ledger(request_id,event) WHERE request_id IS NOT NULL;
CREATE TRIGGER hr_requests_immutable BEFORE UPDATE OR DELETE ON hr.leave_requests FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE TRIGGER hr_leave_immutable BEFORE UPDATE OR DELETE ON hr.leave_ledger FOR EACH ROW EXECUTE FUNCTION hr.reject_mutation();
CREATE VIEW hr.leave_request_state AS SELECT r.*,COALESCE(CASE l.event WHEN 'request' THEN 'pending' WHEN 'approve' THEN 'approved' WHEN 'reject' THEN 'rejected' WHEN 'cancel' THEN 'cancelled' END,'new') AS status
 FROM hr.leave_requests r LEFT JOIN LATERAL(SELECT event FROM hr.leave_ledger WHERE request_id=r.id ORDER BY id DESC LIMIT 1)l ON true;
CREATE FUNCTION hr.working_half_days(first_day DATE,last_day DATE,half TEXT DEFAULT NULL) RETURNS INTEGER LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE WHEN half IS NOT NULL THEN CASE WHEN first_day=last_day AND EXTRACT(ISODOW FROM first_day)<6 THEN 1 ELSE 0 END
 ELSE (SELECT count(*)::integer*2 FROM generate_series(first_day::timestamp,last_day::timestamp,INTERVAL '1 day')d WHERE EXTRACT(ISODOW FROM d)<6) END $$;
CREATE FUNCTION hr.balance(employee BIGINT,kind TEXT) RETURNS BIGINT LANGUAGE sql STABLE AS $$ SELECT COALESCE(sum(half_days),0)::bigint FROM hr.leave_ledger WHERE staff_id=employee AND leave_type=kind $$;
CREATE FUNCTION hr.pending(employee BIGINT,kind TEXT) RETURNS BIGINT LANGUAGE sql STABLE AS $$ SELECT COALESCE(sum(half_days),0)::bigint FROM hr.leave_request_state WHERE staff_id=employee AND leave_type=kind AND status='pending' $$;
CREATE FUNCTION hr.check_leave(employee BIGINT,kind TEXT,first_day DATE,last_day DATE,day_half TEXT,amount INTEGER,except_request BIGINT) RETURNS void LANGUAGE plpgsql AS $$
DECLARE person hr.staff_state%ROWTYPE;
BEGIN
 SELECT * INTO person FROM hr.staff_state WHERE id=employee;
 IF NOT FOUND OR person.status<>'active' OR first_day<person.employment_start_date THEN RAISE EXCEPTION 'leave requires active current employment' USING ERRCODE='23514'; END IF;
 IF amount<>hr.working_half_days(first_day,last_day,day_half) THEN RAISE EXCEPTION 'leave amount must match weekdays/half' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM hr.leave_request_state r WHERE r.staff_id=employee AND r.id<>except_request AND r.status IN('pending','approved')
  AND r.start_date<=last_day AND r.end_date>=first_day AND hr.working_half_days(GREATEST(r.start_date,first_day),LEAST(r.end_date,last_day),NULL)>0
  AND NOT(r.start_date=r.end_date AND first_day=last_day AND r.start_date=first_day AND r.half IS NOT NULL AND day_half IS NOT NULL AND r.half<>day_half)) THEN
  RAISE EXCEPTION 'leave overlaps another request' USING ERRCODE='23514'; END IF;
 IF (SELECT tracks_balance FROM hr.leave_types WHERE code=kind) AND amount>hr.balance(employee,kind)-hr.pending(employee,kind) THEN RAISE EXCEPTION 'insufficient available leave' USING ERRCODE='23514'; END IF;
END $$;
CREATE FUNCTION hr.guard_request() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM hr.lock_resources();
 IF NEW.end_date<NEW.start_date OR NEW.end_date-NEW.start_date>3660 OR NEW.start_date<DATE '1900-01-01' OR NEW.end_date>DATE '9999-12-31' THEN
  RAISE EXCEPTION 'invalid or overlong leave dates' USING ERRCODE='23514'; END IF;
 PERFORM hr.check_leave(NEW.staff_id,NEW.leave_type,NEW.start_date,NEW.end_date,NEW.half,NEW.half_days,NEW.id); RETURN NEW; END $$;
CREATE TRIGGER hr_request_guard BEFORE INSERT ON hr.leave_requests FOR EACH ROW EXECUTE FUNCTION hr.guard_request();
CREATE FUNCTION hr.request_needs_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM hr.leave_ledger WHERE request_id=NEW.id AND event='request') THEN RAISE EXCEPTION 'request requires ledger event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE CONSTRAINT TRIGGER hr_request_event_required AFTER INSERT ON hr.leave_requests DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hr.request_needs_event();
CREATE FUNCTION hr.guard_leave_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE person hr.staff_state%ROWTYPE; q hr.leave_request_state%ROWTYPE; tracked BOOLEAN; allowance INTEGER; first_day DATE; last_day DATE; expected INTEGER;
BEGIN
 PERFORM hr.lock_resources(); SELECT * INTO person FROM hr.staff_state WHERE id=NEW.staff_id;
 IF NOT FOUND OR person.status<>'active' THEN RAISE EXCEPTION 'leave employee is not active' USING ERRCODE='23514'; END IF;
 SELECT tracks_balance INTO tracked FROM hr.leave_types WHERE code=NEW.leave_type;
 IF NEW.request_id IS NULL THEN
  IF NOT tracked THEN RAISE EXCEPTION 'untracked leave has no balance ledger' USING ERRCODE='23514'; END IF;
  IF NEW.event='grant' THEN
   first_day=make_date(NEW.accrual_year,1,1); last_day=make_date(NEW.accrual_year,12,31);
   IF last_day<person.employment_start_date THEN RAISE EXCEPTION 'cannot accrue before employment' USING ERRCODE='23514'; END IF;
   SELECT annual_allowance_half_days INTO allowance FROM hr.leave_type_state WHERE code=NEW.leave_type;
   expected=allowance;
   IF person.employment_start_date>first_day THEN expected=(allowance::bigint*(last_day-person.employment_start_date+1)/(last_day-first_day+1))::integer; END IF;
   IF NEW.half_days<>expected THEN RAISE EXCEPTION 'annual grant must match policy and start-year proration' USING ERRCODE='23514'; END IF;
  ELSIF NEW.event='adjustment' THEN
   IF NEW.half_days=0 OR NEW.reason IS NULL OR length(trim(NEW.reason))=0 THEN RAISE EXCEPTION 'adjustments need an amount and reason' USING ERRCODE='23514'; END IF;
  END IF;
 ELSE
  SELECT * INTO q FROM hr.leave_request_state WHERE id=NEW.request_id;
  IF NOT FOUND OR q.staff_id<>NEW.staff_id OR q.leave_type<>NEW.leave_type THEN RAISE EXCEPTION 'leave request mismatch' USING ERRCODE='23514'; END IF;
  IF NEW.event='request' THEN
   IF q.status<>'new' OR NEW.half_days<>0 THEN RAISE EXCEPTION 'invalid request ledger step' USING ERRCODE='23514'; END IF;
   PERFORM hr.check_leave(q.staff_id,q.leave_type,q.start_date,q.end_date,q.half,q.half_days,q.id);
  ELSIF NEW.event='approve' THEN
   IF q.status<>'pending' OR NEW.half_days<>-q.half_days THEN RAISE EXCEPTION 'invalid approval debit' USING ERRCODE='23514'; END IF;
  ELSIF NEW.event='reject' THEN
   IF q.status<>'pending' OR NEW.half_days<>0 OR NEW.reason IS NULL OR length(trim(NEW.reason))=0 THEN RAISE EXCEPTION 'invalid rejection' USING ERRCODE='23514'; END IF;
  ELSIF NEW.event='cancel' THEN
   IF q.status NOT IN('pending','approved') OR NEW.half_days<>(CASE WHEN q.status='approved' THEN q.half_days ELSE 0 END)
    OR (q.status='approved' AND NEW.as_of>=q.start_date AND NEW.termination_date IS NULL)
    OR q.start_date<person.employment_start_date THEN RAISE EXCEPTION 'invalid cancellation/date' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 IF tracked AND (hr.balance(NEW.staff_id,NEW.leave_type)+NEW.half_days<0 OR
  (NEW.event='adjustment' AND hr.balance(NEW.staff_id,NEW.leave_type)+NEW.half_days<hr.pending(NEW.staff_id,NEW.leave_type))) THEN
  RAISE EXCEPTION 'leave balance cannot go below available' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_leave_guard BEFORE INSERT ON hr.leave_ledger FOR EACH ROW EXECUTE FUNCTION hr.guard_leave_ledger();
CREATE FUNCTION hr.termination_credit_requires_event() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.termination_date IS NOT NULL AND NOT EXISTS(SELECT 1 FROM hr.employment_events WHERE staff_id=NEW.staff_id AND event='terminated' AND effective_date=NEW.termination_date) THEN
  RAISE EXCEPTION 'termination leave settlement requires matching termination event' USING ERRCODE='23514'; END IF; RETURN NEW; END $$;
CREATE CONSTRAINT TRIGGER hr_termination_credit AFTER INSERT ON hr.leave_ledger DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION hr.termination_credit_requires_event();

CREATE FUNCTION hr.guard_employment() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prior hr.employment_events%ROWTYPE; prior_status TEXT; current_status TEXT; first_date DATE; period_date DATE; end_date DATE; location BIGINT; expected_location TEXT; business BIGINT;
BEGIN
 PERFORM hr.lock_resources();
 current_status=NEW.snapshot->>'status'; first_date=(NEW.snapshot->>'start_date')::date; period_date=(NEW.snapshot->>'employment_start_date')::date; end_date=NULLIF(NEW.snapshot->>'end_date','')::date;
 IF current_status NOT IN('active','terminated') OR first_date IS NULL OR period_date IS NULL OR length(trim(COALESCE(NEW.snapshot->>'job_title','')))=0
  OR NEW.snapshot->>'employment_type' NOT IN('full_time','part_time','contractor','temporary','intern')
  OR NOT (NEW.snapshot ? 'status' AND NEW.snapshot ? 'employment_type') THEN RAISE EXCEPTION 'invalid employment snapshot' USING ERRCODE='23514'; END IF;
 location=NULLIF(NEW.snapshot->>'location_record_id','')::bigint;
 IF location IS NOT NULL THEN
  SELECT bindings_json::jsonb->'location'->>'entity' INTO expected_location FROM core.nerd_installations WHERE nerd_id='hr';
  SELECT substring(business_key FROM '^business:([0-9]+)$')::bigint INTO business FROM hr.database_identity WHERE singleton;
  IF NOT EXISTS(SELECT 1 FROM core.entities WHERE id=location AND entity_type=expected_location AND business_id=business) THEN RAISE EXCEPTION 'invalid mapped work location' USING ERRCODE='23514'; END IF;
 END IF;
 SELECT * INTO prior FROM hr.employment_events WHERE staff_id=NEW.staff_id ORDER BY version DESC LIMIT 1;
 IF NOT FOUND THEN
  IF NEW.event<>'hired' OR NEW.version<>1 OR current_status<>'active' OR first_date<>NEW.effective_date OR period_date<>first_date OR end_date IS NOT NULL THEN RAISE EXCEPTION 'first event must hire' USING ERRCODE='23514'; END IF;
 ELSE
  prior_status=prior.snapshot->>'status';
  IF NEW.version<>prior.version+1 OR NEW.effective_date<prior.effective_date OR first_date<>(prior.snapshot->>'start_date')::date THEN RAISE EXCEPTION 'employment versions/dates are monotonic' USING ERRCODE='23514'; END IF;
  IF NEW.event='rehired' THEN
   IF prior_status<>'terminated' OR current_status<>'active' OR NEW.effective_date<=(prior.snapshot->>'end_date')::date OR period_date<>NEW.effective_date OR end_date IS NOT NULL THEN RAISE EXCEPTION 'invalid rehire' USING ERRCODE='23514'; END IF;
  ELSIF NEW.event='terminated' THEN
   IF prior_status<>'active' OR current_status<>'terminated' OR end_date IS DISTINCT FROM NEW.effective_date OR period_date<>(prior.snapshot->>'employment_start_date')::date THEN RAISE EXCEPTION 'invalid termination' USING ERRCODE='23514'; END IF;
   IF EXISTS(SELECT 1 FROM hr.leave_request_state q WHERE q.staff_id=NEW.staff_id AND (q.status='pending' OR (q.status='approved' AND q.end_date>NEW.effective_date))) THEN RAISE EXCEPTION 'settle future leave before termination' USING ERRCODE='23514'; END IF;
  ELSIF NEW.event='updated' THEN
   IF prior_status<>'active' OR current_status<>'active' OR end_date IS NOT NULL OR period_date<>(prior.snapshot->>'employment_start_date')::date THEN RAISE EXCEPTION 'invalid employment update' USING ERRCODE='23514'; END IF;
  ELSE RAISE EXCEPTION 'invalid employment transition' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER hr_employment_guard BEFORE INSERT ON hr.employment_events FOR EACH ROW EXECUTE FUNCTION hr.guard_employment();
