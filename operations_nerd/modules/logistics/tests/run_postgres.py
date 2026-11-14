"""Run only domain acceptance tests on a new disposable native PostgreSQL server.

Usage: python -m operations_nerd.modules.logistics.tests.run_postgres
Requires psycopg[binary], pgserver and its fasteners dependency. No application
environment file, customer database, model, supplier or carrier is accessed.
"""
from pathlib import Path
import os
import tempfile
import unittest


def _run_suite():
    suite = unittest.TestSuite()
    for name in ('test_database', 'test_purchasing', 'test_stock', 'test_host_seams'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName(f'{__package__}.{name}'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() and result.testsRun else 1


def main():
    # A caller may supply an explicitly created disposable database on a shared
    # test server. Each test class verifies the local host/name before resetting.
    if os.environ.get('LOGISTICS_MODULE_TEST_DATABASE_URL'):
        return _run_suite()
    import atexit
    import fasteners
    import pgserver
    import psycopg
    from pgserver.postgres_server import PostgresServer

    with tempfile.TemporaryDirectory(prefix='logistics-module-pg-', dir='/tmp') as directory:
        PostgresServer._lock = fasteners.InterProcessLock(str(Path(directory) / '.server.lock'))
        server = pgserver.get_server(Path(directory) / 'data', cleanup_mode='stop')
        previous = os.environ.get('LOGISTICS_MODULE_TEST_DATABASE_URL')
        try:
            with psycopg.connect(server.get_uri(), autocommit=True) as conn:
                conn.execute('CREATE DATABASE logistics_module_test')
            os.environ['LOGISTICS_MODULE_TEST_DATABASE_URL'] = server.get_uri('logistics_module_test')
            return _run_suite()
        finally:
            if previous is None:
                os.environ.pop('LOGISTICS_MODULE_TEST_DATABASE_URL', None)
            else:
                os.environ['LOGISTICS_MODULE_TEST_DATABASE_URL'] = previous
            server._cleanup()
            atexit.unregister(server._cleanup)


if __name__ == '__main__':
    raise SystemExit(main())
