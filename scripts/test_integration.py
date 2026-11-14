"""Run the integration suites on a new, disposable local PostgreSQL cluster.

This runner never loads .env and never connects to the configured application or
Supabase databases. Install requirements-dev.txt first. The legacy/core offline
suite remains a separate command: python scripts/run_tests.py.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    import atexit
    import pgserver
    import psycopg
    from psycopg import sql
    from urllib.parse import urlsplit, urlunsplit
    with tempfile.TemporaryDirectory(prefix="opsnerd-verification-", dir="/tmp") as directory:
        server = pgserver.get_server(Path(directory) / "postgres", cleanup_mode="stop")
        admin_url = server.get_uri()
        names = ["operations_test_" + uuid4().hex[:16] for _ in range(2)]
        parts = urlsplit(admin_url)
        urls = [urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, "")) for name in names]
        with psycopg.connect(admin_url, autocommit=True) as admin:
            for name in names:
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        environment = dict(os.environ)
        for key in ("OPERATIONS_DATABASE_URL", "OPERATIONS_ADMIN_DATABASE_URL", "OPERATIONS_PG_BIN", "DATABASE_PATH", "APP_ENV"):
            environment.pop(key, None)
        environment.update({"PYTHONPATH": str(ROOT / "operations_nerd"), "LLM_PROVIDER": "stub",
            "OPERATIONS_BACKUP_DIR": str(Path(directory) / "backups"),
            "OPERATIONS_UMBRELLA_ENABLED": "0", "OPERATIONS_TEST_DATABASE_URL": admin_url,
            "OPERATIONS_INTEGRATION_TEST_URL": admin_url, "LOGISTICS_MODULE_TEST_DATABASE_URL": urls[0],
            "OPERATIONS_ADAPTER_TEST_DATABASE_URL": urls[1]})
        commands = [
            [sys.executable, "operations_nerd/tests/test_postgres_foundation.py"],
            [sys.executable, "-m", "operations_nerd.modules.logistics.tests.run_postgres"],
            [sys.executable, "-m", "unittest", "umbrella.tests.test_logistics_adapter", "-v"],
            [sys.executable, "-m", "unittest", "tests_integration.test_umbrella_workflow",
                "tests_integration.test_imports_reports", "tests_integration.test_shipment_imports",
                "tests_integration.test_recovery", "tests_integration.test_support", "-v"],
            [sys.executable, "-m", "unittest", "tests_integration.test_hr", "-v"],
            [sys.executable, "-m", "unittest", "tests_integration.test_accounting", "-v"],
        ]
        failed = []
        try:
            for command in commands:
                result = subprocess.run(command, cwd=ROOT, env=environment)
                if result.returncode:
                    failed.append(command)
        finally:
            # All databases belong to this new cluster. Stop it before removing
            # its directory, including when a suite is interrupted or fails.
            server._cleanup()
            atexit.unregister(server._cleanup)
        print(f"Integration suite groups: {len(commands) - len(failed)}/{len(commands)} passed", flush=True)
        return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
