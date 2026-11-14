"""No model calls: authoritative CRM records through the Logistics host seam."""
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import unittest
from threading import Event
from unittest.mock import patch

from fastapi import HTTPException
from agentic import records, store
from db import db as d
from db.postgres import CORE_WRITE_LOCK
from db.runtime import database_scope
from modules.logistics import core, db, migrate
from modules.logistics.errors import AuthorizationError, Conflict, NotFound, ValidationError
from umbrella import lifecycle, logistics_adapter as adapter
from umbrella.bindings import validate_bindings


BINDINGS = {
    'supplier': {'entity': 'crm_supplier', 'fields': {'name': 'f_supplier_name'}},
    'customer': {'entity': 'crm_customer', 'fields': {'name': 'f_customer_name', 'address': 'f_customer_address'}},
    'item': {'entity': 'crm_item', 'fields': {'name': 'f_item_name', 'sku': 'f_item_sku', 'unit': 'f_item_unit'}},
    'location': {'entity': 'crm_location', 'fields': {'name': 'f_location_name', 'address': 'f_location_address'}},
    'order': {'entity': 'crm_order', 'fields': {'number': 'f_order_number', 'currency': 'f_order_currency'},
              'links': {'customer': 'l_order_customer'}},
    'order_line': {'entity': 'crm_order_line',
        'fields': {'quantity': 'f_line_quantity', 'unit_price_minor': 'f_line_price', 'required_by': 'f_line_required', 'budget_minor': 'f_line_budget'},
        'links': {'order': 'l_line_order', 'item': 'l_line_item'}},
}


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.getenv('OPERATIONS_ADAPTER_TEST_DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('Set OPERATIONS_ADAPTER_TEST_DATABASE_URL to a disposable local operations_test_* database')
        with d.raw_postgres_conn(cls.url) as conn:
            info = conn.execute('SELECT current_database(),inet_server_addr()').fetchone()
            if not info[0].startswith('operations_test_') or (info[1] is not None and str(info[1]) not in ('127.0.0.1', '::1')):
                raise RuntimeError('Adapter tests require an explicitly disposable local database')
        cls.environment = patch.dict(os.environ, {'OPERATIONS_DATABASE_URL': cls.url, 'OPERATIONS_UMBRELLA_ENABLED': '1'})
        cls.environment.start()
        d.init_db(cls.url)
        cls.spec = json.loads((Path(__file__).parents[2]/'agentic/templates/toy_store.json').read_text())

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'environment'):
            cls.environment.stop()

    def setUp(self):
        self.scope = database_scope(self.url)
        self.scope.__enter__()
        with d.raw_postgres_conn() as conn:
            conn.execute('DROP SCHEMA IF EXISTS logistics CASCADE')
            conn.execute('TRUNCATE core.businesses,core.accounts RESTART IDENTITY CASCADE')
        with d.get_conn() as conn:
            lifecycle.ensure_schema(conn)
            conn.execute('DELETE FROM nerd_frozen_records')
            conn.execute('DELETE FROM nerd_installations')
            self.aid = store.create_account(conn, 'owner@example.test', 'test-only')
            self.bid = store.create_business_with_version_1(conn, self.aid, 'Toyland', 'toy_store', self.spec)
        self.item = self.create('crm_item', {'f_item_name': 'Chair', 'f_item_sku': 'CHAIR-1', 'f_item_unit': 'each'})
        self.supplier = self.create('crm_supplier', {'f_supplier_name': 'Supplier A'})
        self.customer = self.create('crm_customer', {'f_customer_name': 'Customer A', 'f_customer_address': '123 Toy Street'})
        self.location = self.create('crm_location', {'f_location_name': 'Warehouse'})
        self.order = self.create('crm_order', {'f_order_number': 'TOY-1', 'f_order_currency': 'USD', 'l_order_customer': self.customer})
        self.line = self.create('crm_order_line', self.line_values())
        with d.get_conn() as conn:
            conn.execute("INSERT INTO nerd_installations(nerd_id,version,status,bindings_json,proposal_id) VALUES('logistics','0.1.0','enabling',?,'test')", (json.dumps(validate_bindings(self.spec, BINDINGS)),))
        with d.raw_postgres_conn() as conn:
            migrate.initialize_database(conn, f'business:{self.bid}')
        adapter.sync_records(self.aid, self.bid, BINDINGS, 'enable:test')
        with d.get_conn() as conn:
            conn.execute("UPDATE nerd_installations SET status='enabled' WHERE nerd_id='logistics'")

    def tearDown(self):
        self.scope.__exit__(None, None, None)

    def create(self, entity, values):
        return records.create_record(self.aid, self.bid, entity, values)['id']

    def line_values(self, **changes):
        values = {'f_line_quantity': 2, 'f_line_price': 1000,
            'f_line_required': (date.today()+timedelta(days=7)).isoformat(),
            'l_line_order': self.order, 'l_line_item': self.item}
        return values | changes

    def counts(self):
        with d.raw_postgres_conn() as conn:
            return conn.execute('''SELECT (SELECT count(*) FROM logistics.demand_lines),
                (SELECT count(*) FROM core.nerd_frozen_records),
                (SELECT count(*) FROM logistics.command_requests)''').fetchone()

    def test_read_workspace_uses_host_records_and_never_synchronizes(self):
        before = self.counts()
        result = adapter.workspace(self.aid, self.bid, BINDINGS)
        self.assertEqual(self.counts(), before)
        self.assertEqual(result['orders'][0]['order_number'], 'TOY-1')
        self.assertEqual(result['orders'][0]['customer_name'], 'Customer A')
        self.assertFalse(result['orders'][0]['submitted'])
        records.update_record(self.aid, self.bid, 'crm_item', self.item, {'f_item_name': 'Renamed chair'})
        changed = adapter.workspace(self.aid, self.bid, BINDINGS)
        item = next(row for row in changed['references'] if row['kind'] == 'item')
        self.assertEqual(item['snapshot']['name'], 'Renamed chair')
        with d.raw_postgres_conn() as conn:
            self.assertIsNone(conn.execute("SELECT to_regclass('logistics.record_items')").fetchone()[0])
            self.assertIsNone(conn.execute("SELECT to_regclass('logistics.record_customer_orders')").fetchone()[0])

    def test_order_submission_replays_and_freezes_header_lines_and_new_links(self):
        first = adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'order-submit')
        self.assertEqual(first, adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'order-submit'))
        self.assertEqual(first['demands'][0]['quantity'], 2)
        self.assertEqual(self.counts()[:2], (1, 2))
        with self.assertRaises(Conflict):
            adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'second-submit')
        for entity, identifier, changes in (
            ('crm_order', self.order, {'f_order_number': 'changed'}),
            ('crm_order_line', self.line, {'f_line_quantity': 3}),
        ):
            with self.assertRaises(records.RecordError) as error:
                records.update_record(self.aid, self.bid, entity, identifier, changes)
            self.assertEqual(error.exception.status, 409)
        with self.assertRaises(records.RecordError) as error:
            self.create('crm_order_line', self.line_values())
        self.assertEqual(error.exception.status, 409)

    def test_invalid_later_line_rolls_back_demand_freezing_and_request(self):
        second = self.create('crm_order_line', self.line_values(f_line_quantity=3))
        with d.get_conn() as conn:
            d.set_entity_attributes(conn, 'crm_order_line', second, {'f_line_quantity': 1.5})
        before = self.counts()
        with self.assertRaises(ValidationError):
            adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'fixable-order')
        self.assertEqual(self.counts(), before)
        records.update_record(self.aid, self.bid, 'crm_order_line', second, {'f_line_quantity': 3})
        result = adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'fixable-order')
        self.assertEqual(len(result['demands']), 2)

    def test_raw_operational_numbers_are_exact_before_generic_coercion(self):
        for value in ('1,5', '1.5', '1e3', 1.5, True):
            with self.assertRaises(records.RecordError) as error:
                records.update_record(self.aid, self.bid, 'crm_order_line', self.line, {'f_line_quantity': value})
            self.assertEqual(error.exception.status, 422)
        result = records.update_record(self.aid, self.bid, 'crm_order_line', self.line,
            {'f_line_quantity': '9007199254740993'})
        self.assertEqual(result['values']['f_line_quantity'], 9007199254740993)
        self.assertIs(type(result['values']['f_line_quantity']), int)

    def test_item_sku_unique_and_unit_immutable_after_operational_use(self):
        with self.assertRaises(records.RecordError):
            self.create('crm_item', {'f_item_name': 'Other', 'f_item_sku': 'CHAIR-1', 'f_item_unit': 'each'})
        adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'use-item')
        with self.assertRaises(records.RecordError):
            records.update_record(self.aid, self.bid, 'crm_item', self.item, {'f_item_unit': 'box'})
        records.update_record(self.aid, self.bid, 'crm_item', self.item, {'f_item_name': 'Display rename'})

    def test_projected_item_identity_is_immutable_before_use_and_after_disable(self):
        self.assertEqual(self.counts()[0], 0)
        for changes in ({'f_item_unit': 'box'}, {'f_item_sku': 'REINTERPRETED'}):
            with self.assertRaises(records.RecordError) as error:
                records.update_record(self.aid, self.bid, 'crm_item', self.item, changes)
            self.assertEqual(error.exception.status, 422)
        with d.get_conn() as conn:
            conn.execute("UPDATE nerd_installations SET status='disabled' WHERE nerd_id='logistics'")
        for changes in ({'f_item_unit': 'box'}, {'f_item_sku': 'REINTERPRETED'}):
            with self.assertRaises(records.RecordError) as error:
                records.update_record(self.aid, self.bid, 'crm_item', self.item, changes)
            self.assertEqual(error.exception.status, 422)
        updated = records.update_record(self.aid, self.bid, 'crm_item', self.item, {'f_item_name': 'Still editable display name'})
        self.assertEqual(updated['values']['f_item_name'], 'Still editable display name')

    def test_strict_command_allowlist_and_server_actor(self):
        for operation, payload in (
            ('bind_reference', {'kind': 'actor'}),
            ('approve_purchase_order', {'purchase_order_id': 1, 'actor_id': 999, 'idempotency_key': 'injected'}),
            ('create_purchase_order', {'supplier_id': True, 'currency': 'USD', 'idempotency_key': 'bool'}),
        ):
            with self.assertRaises(ValidationError):
                adapter.invoke(self.aid, self.bid, BINDINGS, operation, payload)
        with self.assertRaises(NotFound):
            adapter.workspace(self.aid+999, self.bid, BINDINGS)
        with self.assertRaises(AuthorizationError):
            adapter.workspace(self.aid, self.bid+999, BINDINGS)

    def test_supplier_revision_keeps_historical_snapshot(self):
        first = adapter.workspace(self.aid, self.bid, BINDINGS)
        old = next(row for row in first['references'] if row['kind'] == 'supplier')
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'Supplier renamed'})
        adapter.sync_records(self.aid, self.bid, BINDINGS, 'supplier-revision')
        result = adapter.workspace(self.aid, self.bid, BINDINGS)
        revisions = [row for row in result['references'] if row['kind'] == 'supplier']
        self.assertEqual(len(revisions), 2)
        self.assertEqual(next(row for row in revisions if row['id'] == old['id'])['snapshot']['name'], 'Supplier A')
        self.assertEqual(next(row for row in revisions if row['valid_to'] is None)['snapshot']['name'], 'Supplier renamed')

    def test_supplier_sync_retries_and_a_b_a_under_same_parent_request_key(self):
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'Supplier B'})
        adapter.sync_records(self.aid, self.bid, BINDINGS, 'one-parent-sync')
        before_replay = self.counts()
        adapter.sync_records(self.aid, self.bid, BINDINGS, 'one-parent-sync')
        self.assertEqual(self.counts(), before_replay)
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'Supplier A'})
        adapter.sync_records(self.aid, self.bid, BINDINGS, 'one-parent-sync')
        workspace = adapter.workspace(self.aid, self.bid, BINDINGS)
        suppliers = [row for row in workspace['references'] if row['kind'] == 'supplier']
        self.assertEqual([row['snapshot']['name'] for row in suppliers], ['Supplier A', 'Supplier B', 'Supplier A'])
        self.assertEqual([row['valid_to'] is None for row in suppliers], [False, False, True])

    def test_commands_require_authoritative_current_references_but_preserve_replay(self):
        result = adapter.workspace(self.aid, self.bid, BINDINGS)
        supplier = next(row['id'] for row in result['references'] if row['kind'] == 'supplier')
        location = next(row['id'] for row in result['references'] if row['kind'] == 'location')
        payload = {'supplier_id': supplier, 'currency': 'USD', 'idempotency_key': 'draft-once'}
        first = adapter.invoke(self.aid, self.bid, BINDINGS, 'create_purchase_order', payload)
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'New current supplier name'})
        self.assertEqual(adapter.invoke(self.aid, self.bid, BINDINGS, 'create_purchase_order', payload), first)
        with self.assertRaises(Conflict):
            adapter.invoke(self.aid, self.bid, BINDINGS, 'create_purchase_order', payload | {'idempotency_key': 'stale-draft'})
        with d.raw_postgres_conn() as conn:
            db.prepare_connection(conn, f'business:{self.bid}')
            fake = core.bind_reference(conn, 'item', 'local:items:999', {'name': 'Unmapped item'}, 'trusted-test-only')
        with self.assertRaises(ValidationError):
            adapter.invoke(self.aid, self.bid, BINDINGS, 'open_stock', {'item_id': fake['id'], 'location_id': location,
                'quantity': 2, 'source_ref': 'opening count', 'idempotency_key': 'unmapped-opening'})

    def test_failed_commands_release_host_session_lock(self):
        with self.assertRaises(ValidationError):
            adapter.invoke(self.aid, self.bid, BINDINGS, 'create_shipment',
                {'shipment_number': 'bad', 'order_ref': 'missing', 'idempotency_key': 'bad-shipment'})
        with d.raw_postgres_conn() as conn:
            self.assertTrue(conn.execute('SELECT pg_try_advisory_lock(%s)', (CORE_WRITE_LOCK,)).fetchone()[0])
            conn.execute('SELECT pg_advisory_unlock(%s)', (CORE_WRITE_LOCK,))

    def test_disable_retains_history_and_reenable_rejects_role_reinterpretation(self):
        workspace = adapter.workspace(self.aid, self.bid, BINDINGS)
        item = next(row['id'] for row in workspace['references'] if row['kind'] == 'item')
        location = next(row['id'] for row in workspace['references'] if row['kind'] == 'location')
        adapter.invoke(self.aid, self.bid, BINDINGS, 'open_stock', {'item_id': item, 'location_id': location,
            'quantity': 4, 'source_ref': 'physical count', 'idempotency_key': 'opening'})
        adapter.submit_order(self.aid, self.bid, BINDINGS, self.order, 'frozen-order')
        before = self.counts()[:2]
        disabled = lifecycle.disable(self.aid, self.bid)
        self.assertTrue(disabled['data_retained'])
        with self.assertRaises(Conflict):
            adapter.workspace(self.aid, self.bid, BINDINGS)
        changed = json.loads(json.dumps(BINDINGS))
        changed['item']['fields']['name'], changed['item']['fields']['unit'] = 'f_item_unit', 'f_item_name'
        with self.assertRaises(HTTPException) as error:
            lifecycle.propose(self.aid, self.bid, 1, changed)
        self.assertEqual(error.exception.status_code, 409)
        proposal = lifecycle.propose(self.aid, self.bid, 1, BINDINGS)
        enabled = lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(enabled['status'], 'enabled')
        self.assertEqual(self.counts()[:2], before)
        restored = adapter.workspace(self.aid, self.bid, BINDINGS)
        self.assertEqual(restored['stock'][0]['on_hand'], 4)
        self.assertEqual(len(restored['submitted_orders']), 1)
        with d.raw_postgres_conn() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM logistics.schema_migrations').fetchone()[0], 3)
        lifecycle.disable(self.aid, self.bid)
        with self.assertRaises(HTTPException) as error:
            lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(error.exception.status_code, 409)

    def test_failed_and_interrupted_enablement_retry_without_resetting_history(self):
        lifecycle.disable(self.aid, self.bid)
        proposal = lifecycle.propose(self.aid, self.bid, 1, BINDINGS)
        with patch.object(migrate, 'initialize_database', side_effect=RuntimeError('injected test failure')):
            with self.assertRaises(HTTPException) as error:
                lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(next(row for row in lifecycle.describe(self.aid, self.bid)['nerds'] if row['id'] == 'logistics')['status'], 'failed')
        before = self.counts()[:2]
        retried = lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(retried['status'], 'enabled')
        self.assertEqual(self.counts()[:2], before)
        lifecycle.disable(self.aid, self.bid)
        interrupted = lifecycle.propose(self.aid, self.bid, 1, BINDINGS)
        with patch.object(adapter, 'sync_records', side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                lifecycle.approve(self.aid, self.bid, interrupted['id'])
        self.assertEqual(next(row for row in lifecycle.describe(self.aid, self.bid)['nerds'] if row['id'] == 'logistics')['status'], 'enabling')
        self.assertEqual(lifecycle.approve(self.aid, self.bid, interrupted['id'])['status'], 'enabled')
        self.assertEqual(self.counts()[:2], before)

    def test_failed_enable_retries_after_committed_supplier_projection_and_source_change(self):
        lifecycle.disable(self.aid, self.bid)
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'Supplier B'})
        proposal = lifecycle.propose(self.aid, self.bid, 1, BINDINGS)
        original = core.bind_reference

        def fail_after_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1] == 'supplier':
                raise RuntimeError('Injected interruption after the child commit')
            return result

        with patch.object(core, 'bind_reference', side_effect=fail_after_commit):
            with self.assertRaises(HTTPException) as error:
                lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(next(row for row in lifecycle.describe(self.aid, self.bid)['nerds'] if row['id'] == 'logistics')['status'], 'failed')
        records.update_record(self.aid, self.bid, 'crm_supplier', self.supplier, {'f_supplier_name': 'Supplier C'})
        self.assertEqual(lifecycle.approve(self.aid, self.bid, proposal['id'])['status'], 'enabled')
        suppliers = [row for row in adapter.workspace(self.aid, self.bid, BINDINGS)['references'] if row['kind'] == 'supplier']
        self.assertEqual([row['snapshot']['name'] for row in suppliers], ['Supplier A', 'Supplier B', 'Supplier C'])
        self.assertEqual([row['valid_to'] is None for row in suppliers], [False, False, True])

    def test_schema_revert_locks_compatibility_read_against_concurrent_enablement(self):
        from agentic import chat
        from umbrella import bindings as role_bindings
        lifecycle.disable(self.aid, self.bid)
        historical = json.loads(json.dumps(self.spec))
        for entity in historical['entities']:
            if entity['key'] == 'crm_item':
                entity['fields'] = [field for field in entity['fields'] if field['key'] != 'f_item_unit']
        # Build a real append-only history: a disabled module permits removing
        # the field, then restoring it. Revert will target that older version.
        with d.get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            store.append_version(conn, self.bid, 1, historical, [], 'test', self.aid)
            store.append_version(conn, self.bid, 2, self.spec, [], 'test', self.aid)
        proposal = lifecycle.propose(self.aid, self.bid, 3, BINDINGS)
        checked, release, enable_started, enable_finished = Event(), Event(), Event(), Event()
        original = role_bindings.compatibility_errors

        def pause_after_read(conn, business_id, spec):
            result = original(conn, business_id, spec)
            checked.set()
            if not release.wait(5):
                raise RuntimeError('Timed out releasing the controlled compatibility read')
            return result

        def revert():
            with database_scope(self.url):
                return chat.revert(self.aid, self.bid, 2)

        def enable():
            enable_started.set()
            try:
                with database_scope(self.url):
                    return lifecycle.approve(self.aid, self.bid, proposal['id'])
            except HTTPException as error:
                return {'http_status': error.status_code}
            finally:
                enable_finished.set()

        early_lock = None
        with patch.object(role_bindings, 'compatibility_errors', side_effect=pause_after_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                revert_future = pool.submit(revert)
                try:
                    self.assertTrue(checked.wait(3))
                    with d.raw_postgres_conn() as observer:
                        early_lock = observer.execute('SELECT pg_try_advisory_lock(%s)', (CORE_WRITE_LOCK,)).fetchone()[0]
                        if early_lock:
                            observer.execute('SELECT pg_advisory_unlock(%s)', (CORE_WRITE_LOCK,))
                    enable_future = pool.submit(enable)
                    self.assertTrue(enable_started.wait(3))
                    completed_before_release = enable_finished.wait(0.2)
                finally:
                    # Always unblock the writer, including failed assertions.
                    release.set()
                reverted = revert_future.result(timeout=5)
                enabled = enable_future.result(timeout=5)
        self.assertFalse(early_lock, 'Schema compatibility must be checked while holding the core write lock')
        self.assertFalse(completed_before_release, 'Enablement must wait for the already checked schema write')
        self.assertEqual(reverted['version'], 4)
        self.assertEqual(enabled, {'http_status': 409})
        self.assertEqual(next(row for row in lifecycle.describe(self.aid, self.bid)['nerds'] if row['id'] == 'logistics')['status'], 'disabled')

    def test_failed_reenable_cannot_retarget_existing_history_or_a_stale_proposal(self):
        lifecycle.disable(self.aid, self.bid)
        proposal = lifecycle.propose(self.aid, self.bid, 1, BINDINGS)
        changed = json.loads(json.dumps(BINDINGS))
        changed['item']['fields']['name'], changed['item']['fields']['unit'] = 'f_item_unit', 'f_item_name'
        stale_id = 'previously-reviewed-different-mapping'
        # Model a previously reviewed pending setup, created before installation
        # history existed. Applying it must still recheck the current invariant.
        with d.get_conn() as conn:
            conn.execute('''INSERT INTO nerd_proposals(id,business_id,base_version,bindings_json,status,nerd_id)
                VALUES(?,?,1,?,'pending','logistics')''',
                (stale_id, self.bid, json.dumps(validate_bindings(self.spec, changed))))
        with patch.object(migrate, 'initialize_database', side_effect=RuntimeError('failed re-enable migration')):
            with self.assertRaises(HTTPException) as error:
                lifecycle.approve(self.aid, self.bid, proposal['id'])
        self.assertEqual(error.exception.status_code, 503)
        with self.assertRaises(HTTPException) as error:
            lifecycle.propose(self.aid, self.bid, 1, changed)
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(HTTPException) as error:
            lifecycle.approve(self.aid, self.bid, stale_id)
        self.assertEqual(error.exception.status_code, 409)
        with d.get_conn() as conn:
            stored = conn.execute("SELECT bindings_json FROM nerd_installations WHERE nerd_id='logistics'").fetchone()
            self.assertEqual(json.loads(stored['bindings_json']), validate_bindings(self.spec, BINDINGS))
        self.assertEqual(lifecycle.approve(self.aid, self.bid, proposal['id'])['status'], 'enabled')
