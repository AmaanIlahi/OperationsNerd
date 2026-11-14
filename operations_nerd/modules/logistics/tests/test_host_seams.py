from pathlib import Path
import hashlib
import tempfile

import psycopg

from .. import db, fulfillment
from ..errors import AuthorizationError, ConfigurationError
from ..migrate import initialize_database, MIGRATIONS, SOURCE_CHECKSUMS
from .common import PostgresCase


class HostSeamTests(PostgresCase):
    def test_source_migrations_are_pinned_and_replay_is_empty(self):
        for name, expected in SOURCE_CHECKSUMS.items():
            self.assertEqual(hashlib.sha256((MIGRATIONS/name).read_bytes()).hexdigest(), expected)
        self.assertEqual(initialize_database(self.conn, 'test-business'), [])
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM logistics.schema_migrations').fetchone()['n'], 3)
        with self.assertRaises(AuthorizationError):
            initialize_database(self.conn, 'different-business')

    def test_migration_sources_cannot_be_omitted_or_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with self.assertRaises(ConfigurationError):
                initialize_database(self.conn, 'test-business', migrations=target)
            for path in MIGRATIONS.glob('*.sql'):
                (target/path.name).write_bytes(path.read_bytes())
            name = next(iter(SOURCE_CHECKSUMS))
            (target/name).write_bytes((target/name).read_bytes()+b'\n-- changed\n')
            with self.assertRaises(ConfigurationError):
                initialize_database(self.conn, 'test-business', migrations=target)

    def test_host_connection_gets_module_search_path_and_private_identity_check(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute('SET search_path TO public')
            db.prepare_connection(conn, 'test-business')
            self.assertEqual(conn.execute('SELECT business_key FROM database_identity').fetchone()['business_key'], 'test-business')
        with self.assertRaises(AuthorizationError):
            db.prepare_connection(self.conn, 'other-business')

    def test_public_commands_and_migrations_reject_nested_host_transactions(self):
        with self.conn.transaction():
            for callback in (
                lambda: db.prepare_connection(self.conn),
                lambda: initialize_database(self.conn, 'test-business'),
                lambda: db.run_command(self.conn, 'nested', 'nested', {}, lambda c: {}),
            ):
                with self.assertRaises(ConfigurationError):
                    callback()
        with psycopg.connect(self.dsn, autocommit=False) as conn:
            with self.assertRaises(ConfigurationError):
                db.prepare_connection(conn)

    def test_fulfillment_uses_host_labels_without_standalone_record_tables(self):
        item = self.bind('item', 'core:item:42', {'name': 'Historical chair'})
        self.bind('location', 'core:location:10', {'name': 'Warehouse'})
        self.demand(item, 2, order_ref='core:order:77')
        self.assertIsNone(self.conn.execute("SELECT to_regclass('logistics.record_customer_orders') AS name").fetchone()['name'])
        seen = []

        class HostDisplay:
            def order_labels(adapter, conn, order_refs):
                seen.extend(order_refs)
                return {'core:order:77': {'order_number': 'TOY-77', 'customer_name': 'Toyland', 'ship_to_address': 'Host address'}}

            def decorate_references(adapter, conn, references):
                return [dict(row, snapshot=dict(row['snapshot']) | {'name': 'Host display name'}) for row in references]

        workspace = fulfillment.workspace(self.conn, display=HostDisplay())
        self.assertEqual(seen, ['core:order:77'])
        self.assertEqual(workspace['orders'][0]['customer_name'], 'Toyland')
        self.assertEqual(workspace['orders'][0]['ship_to_address'], 'Host address')
        self.assertEqual(workspace['orders'][0]['order_number'], 'TOY-77')
        self.assertEqual(workspace['references'][0]['snapshot']['name'], 'Host display name')
        stored = self.conn.execute('SELECT snapshot FROM core_bindings WHERE id=%s', (item,)).fetchone()
        self.assertEqual(stored['snapshot']['name'], 'Historical chair')
        fallback = fulfillment.workspace(self.conn)
        self.assertEqual(fallback['orders'][0]['order_number'], 'core:order:77')
        self.assertEqual(fallback['references'][0]['snapshot']['name'], 'Historical chair')
