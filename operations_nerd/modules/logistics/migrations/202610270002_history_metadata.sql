SET search_path TO logistics, public;

-- Business and migration identity cannot be silently reassigned or rewritten.
CREATE TRIGGER database_identity_no_update BEFORE UPDATE OR DELETE ON database_identity
 FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER migration_history_no_update BEFORE UPDATE OR DELETE ON schema_migrations
 FOR EACH ROW EXECUTE FUNCTION reject_mutation();
ALTER TABLE core_bindings ADD CONSTRAINT core_snapshot_object CHECK(jsonb_typeof(snapshot)='object');
CREATE FUNCTION guard_binding_intervals() RETURNS trigger LANGUAGE plpgsql SET search_path=logistics,pg_temp AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(1768842363,hashtext(NEW.kind||':'||NEW.external_key));
 IF EXISTS(SELECT 1 FROM core_bindings b WHERE b.kind=NEW.kind AND b.external_key=NEW.external_key
   AND tstzrange(b.valid_from,b.valid_to,'[)') && tstzrange(NEW.valid_from,NEW.valid_to,'[)')) THEN
  RAISE EXCEPTION 'core reference validity intervals overlap' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER core_binding_intervals BEFORE INSERT ON core_bindings
 FOR EACH ROW EXECUTE FUNCTION guard_binding_intervals();
