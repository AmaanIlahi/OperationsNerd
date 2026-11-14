-- Owner-managed business records. Application metadata creates typed physical
-- tables in this private schema; references are PostgreSQL foreign keys.
SET search_path TO logistics, public;

-- Typed projections retain the host adapter seam while the database itself
-- rejects a location/item ID accidentally passed as a supplier or actor ID.
ALTER TABLE core_bindings ADD CONSTRAINT core_binding_typed_identity UNIQUE(id,kind);
ALTER TABLE purchase_orders
 ADD COLUMN _supplier_kind text GENERATED ALWAYS AS ('supplier'::text) STORED,
 ADD COLUMN _actor_kind text GENERATED ALWAYS AS ('actor'::text) STORED,
 ADD CONSTRAINT po_supplier_kind_fk FOREIGN KEY(supplier_id,_supplier_kind) REFERENCES core_bindings(id,kind),
 ADD CONSTRAINT po_actor_kind_fk FOREIGN KEY(approved_by,_actor_kind) REFERENCES core_bindings(id,kind);
ALTER TABLE quote_versions
 ADD COLUMN _supplier_kind text GENERATED ALWAYS AS ('supplier'::text) STORED,
 ADD COLUMN _item_kind text GENERATED ALWAYS AS ('item'::text) STORED,
 ADD CONSTRAINT quote_supplier_kind_fk FOREIGN KEY(supplier_id,_supplier_kind) REFERENCES core_bindings(id,kind),
 ADD CONSTRAINT quote_item_kind_fk FOREIGN KEY(item_id,_item_kind) REFERENCES core_bindings(id,kind);
ALTER TABLE purchase_order_lines
 ADD COLUMN _item_kind text GENERATED ALWAYS AS ('item'::text) STORED,
 ADD CONSTRAINT purchase_item_kind_fk FOREIGN KEY(item_id,_item_kind) REFERENCES core_bindings(id,kind);
ALTER TABLE demand_lines
 ADD COLUMN _item_kind text GENERATED ALWAYS AS ('item'::text) STORED,
 ADD CONSTRAINT demand_item_kind_fk FOREIGN KEY(item_id,_item_kind) REFERENCES core_bindings(id,kind);

CREATE TABLE business_setup (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 business_name text NOT NULL CHECK(length(trim(business_name))>0),
 currency text NOT NULL CHECK(currency ~ '^[A-Z]{3}$'),
 business_type text NOT NULL DEFAULT '', uses_inventory boolean NOT NULL DEFAULT true,
 setup_notes text NOT NULL DEFAULT '', version integer NOT NULL DEFAULT 1 CHECK(version>0),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE record_tables (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 slug text NOT NULL UNIQUE CHECK(slug ~ '^[a-z][a-z0-9_]{0,39}$'),
 label text NOT NULL CHECK(length(trim(label))>0),
 role text NOT NULL DEFAULT 'custom' CHECK(role IN
 ('custom','supplier','customer','item','location','carrier','employee','customer_order','customer_order_line')),
 version integer NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE record_fields (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 table_id bigint NOT NULL REFERENCES record_tables(id),
 slug text NOT NULL CHECK(slug ~ '^[a-z][a-z0-9_]{0,39}$'),
 label text NOT NULL CHECK(length(trim(label))>0),
 type text NOT NULL CHECK(type IN ('text','integer','money','boolean','date','reference')),
 required boolean NOT NULL DEFAULT false, is_unique boolean NOT NULL DEFAULT false,
 target_table_id bigint REFERENCES record_tables(id), min_value bigint,
 position integer NOT NULL CHECK(position>=0), UNIQUE(table_id,slug),
 CHECK((type='reference')=(target_table_id IS NOT NULL)),
 CHECK(min_value IS NULL OR type IN ('integer','money'))
);
CREATE TABLE record_binding_links (
 binding_id bigint PRIMARY KEY REFERENCES core_bindings(id),
 table_id bigint NOT NULL REFERENCES record_tables(id), record_id bigint NOT NULL CHECK(record_id>0)
);
CREATE INDEX record_binding_record ON record_binding_links(table_id,record_id);
CREATE TABLE record_events (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 table_id bigint NOT NULL REFERENCES record_tables(id), record_id bigint NOT NULL CHECK(record_id>0),
 event text NOT NULL CHECK(event IN ('created','updated','archived','restored','deleted','submitted')),
 snapshot jsonb NOT NULL CHECK(jsonb_typeof(snapshot)='object'),
 request_key text NOT NULL REFERENCES command_requests(idempotency_key) DEFERRABLE INITIALLY DEFERRED,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER record_events_immutable BEFORE UPDATE OR DELETE ON record_events
 FOR EACH ROW EXECUTE FUNCTION reject_mutation();

-- A local binding may only be used for a new commercial action while its
-- owning business record is active. Historic references remain intact.
CREATE FUNCTION require_active_local_binding(binding bigint) RETURNS void
 LANGUAGE plpgsql SET search_path=logistics,pg_temp AS $$
DECLARE owner record; active boolean;
BEGIN
 SELECT t.slug,l.record_id INTO owner FROM record_binding_links l
 JOIN record_tables t ON t.id=l.table_id WHERE l.binding_id=binding;
 IF FOUND THEN
  EXECUTE format('SELECT NOT archived FROM logistics.%I WHERE id=$1 FOR SHARE','record_'||owner.slug)
    INTO active USING owner.record_id;
  IF active IS DISTINCT FROM true THEN
   RAISE EXCEPTION 'local business reference is archived or deleted' USING ERRCODE='23514';
  END IF;
 END IF;
END; $$;
CREATE FUNCTION guard_local_purchase_references() RETURNS trigger
 LANGUAGE plpgsql SET search_path=logistics,pg_temp AS $$
BEGIN
 PERFORM require_active_local_binding(NEW.supplier_id);
 IF TG_TABLE_NAME='quote_versions' THEN PERFORM require_active_local_binding(NEW.item_id); END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER local_po_reference_guard BEFORE INSERT OR UPDATE ON purchase_orders
 FOR EACH ROW EXECUTE FUNCTION guard_local_purchase_references();
CREATE TRIGGER local_quote_reference_guard BEFORE INSERT ON quote_versions
 FOR EACH ROW EXECUTE FUNCTION guard_local_purchase_references();

-- These guards are attached to the standard order tables when the owner
-- completes setup. Submission publishes immutable demand and freezes drafts.
CREATE FUNCTION guard_submitted_customer_order() RETURNS trigger
 LANGUAGE plpgsql SET search_path=logistics,pg_temp AS $$
BEGIN
 IF OLD._submitted_at IS NOT NULL THEN
  RAISE EXCEPTION 'submitted customer order is frozen' USING ERRCODE='23514';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;
CREATE FUNCTION guard_submitted_customer_line() RETURNS trigger
 LANGUAGE plpgsql SET search_path=logistics,pg_temp AS $$
DECLARE current_order bigint; old_order bigint; submitted timestamptz;
BEGIN
 IF TG_OP<>'DELETE' THEN current_order:=NEW.order_id; END IF;
 IF TG_OP<>'INSERT' THEN old_order:=OLD.order_id; END IF;
 -- Sort parent locks so moves between draft orders cannot deadlock each other.
 FOR submitted IN SELECT _submitted_at FROM record_customer_orders
  WHERE id IN(current_order,old_order) ORDER BY id FOR UPDATE LOOP
  IF submitted IS NOT NULL THEN
   RAISE EXCEPTION 'submitted customer order lines are frozen' USING ERRCODE='23514';
  END IF;
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END; $$;

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
  REVOKE ALL ON business_setup,record_tables,record_fields,record_binding_links,record_events FROM anon;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
  REVOKE ALL ON business_setup,record_tables,record_fields,record_binding_links,record_events FROM authenticated;
 END IF;
END $$;
