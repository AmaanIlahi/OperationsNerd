"""Destructive test fixtures only for explicitly named disposable local DBs."""
from datetime import date, timedelta
from itertools import count
import os
import unittest

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from ..core import bind_reference, register_demand
from ..db import connect
from ..migrate import initialize_database


class PostgresCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ.get('LOGISTICS_MODULE_TEST_DATABASE_URL', '')
        if not cls.dsn:
            raise unittest.SkipTest('Run the logistics.tests.run_postgres disposable runner')
        with connect(cls.dsn) as conn:
            info = conn.execute('SELECT current_database() AS name,inet_server_addr() AS address').fetchone()
            host = conninfo_to_dict(cls.dsn).get('host', '')
            local = info['address'] is None or host in ('127.0.0.1', '::1', 'localhost')
            if not info['name'].startswith(('logistics_module_test', 'operations_test_')) or not local:
                raise RuntimeError('Test resets require a disposable local logistics_module_test database')
            initialize_database(conn, 'test-business')

    def setUp(self):
        self.conn = connect(self.dsn, 'test-business')
        tables = self.conn.execute("""SELECT tablename FROM pg_tables WHERE schemaname='logistics'
            AND tablename NOT IN ('database_identity','schema_migrations')""").fetchall()
        self.conn.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(
            sql.SQL(',').join(sql.Identifier('logistics', row['tablename']) for row in tables)))
        self._keys = count(1)

    def tearDown(self):
        self.conn.close()

    def key(self, prefix='test'):
        return f'{prefix}-{next(self._keys)}'

    def newconn(self):
        return connect(self.dsn, 'test-business')

    def bind(self, kind, key, snapshot=None):
        return bind_reference(self.conn, kind, key, snapshot or {'name': key}, self.key('bind'))['id']

    def demand(self, item_id, quantity, order_ref='order-1', budget_minor=None, required_by=None):
        key = self.key('demand')
        return register_demand(self.conn, external_key=key, order_ref=order_ref, item_id=item_id,
            quantity=quantity, currency='USD', required_by=required_by or date.today()+timedelta(days=30),
            budget_minor=budget_minor, idempotency_key=key)['id']
