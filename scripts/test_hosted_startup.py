"""Offline hosted-startup regressions; no model, database or Docker required."""
from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import check_hosted as check
import start_hosted as start


class HostedStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hosted-preflight-test-')
        self.backup = Path(self.temporary.name) / 'private-backups'
        self.environment = patch.dict(os.environ, {
            'APP_ENV': 'production', 'OPERATIONS_UMBRELLA_ENABLED': '1',
            'SESSION_SECRET': 'fixture-session-secret-with-more-than-32-characters',
            'SIGNUP_INVITE_CODE': 'fixture-private-invite', 'LLM_PROVIDER': 'stub',
            'TRUSTED_PROXIES': '127.0.0.1', 'OPERATIONS_BACKUP_DIR': str(self.backup),
            'OPERATIONS_DATABASE_URL': 'postgresql://app:fixture-password@db:5432/app_control',
        }, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_explicit_production_configuration_accepts_only_matching_admin_origin(self):
        self.assertEqual(check.validate_environment()['provider'], 'stub')
        with patch.dict(os.environ, {'OPERATIONS_ADMIN_DATABASE_URL': 'postgresql://app:fixture-password@db:5432/postgres'}):
            check.validate_environment()
        for url in ('postgresql://other:fixture-password@db:5432/postgres',
                    'postgresql://app:fixture-password@other-server:5432/postgres'):
            with self.subTest(url=url), patch.dict(os.environ, {'OPERATIONS_ADMIN_DATABASE_URL': url}):
                with self.assertRaisesRegex(check.HostedConfigurationError, 'same PostgreSQL server and owning role'):
                    check.validate_environment()

    def test_refuses_development_or_open_configuration_before_connecting(self):
        cases = [('APP_ENV', 'development'), ('OPERATIONS_UMBRELLA_ENABLED', '0'),
                 ('ALLOW_OPEN_SIGNUP', '1'), ('SIGNUP_INVITE_CODE', 'local-demo'),
                 ('RATE_LIMIT_ENABLED', '0'), ('SESSION_SECRET', ''),
                 ('LLM_PROVIDER', ''), ('TRUSTED_PROXIES', ''), ('CONFIG_AGENT_ENABLED', '1')]
        for key, value in cases:
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), \
                 patch.object(check, 'check_database') as connect:
                with self.assertRaises(check.HostedConfigurationError):
                    check.preflight()
                connect.assert_not_called()

    def test_live_provider_requires_key_without_making_model_request(self):
        with patch.dict(os.environ, {'LLM_PROVIDER': 'baseten'}):
            with self.assertRaisesRegex(check.HostedConfigurationError, 'BASETEN_API_KEY'):
                check.validate_environment()

    def test_invalid_connection_values_are_never_in_errors(self):
        secret = 'fixture-do-not-print-this-password'
        for url in ('sqlite:///tmp/app.db', f'postgresql://app:{secret}@db:bad-port/control',
                    f'postgresql://app:{secret}@pooler.example:5432/control',
                    f'postgresql://app:{secret}@db:6543/control'):
            with self.subTest(kind=url.split(':', 1)[0]), patch.dict(os.environ, {'OPERATIONS_DATABASE_URL': url}):
                with self.assertRaises(check.HostedConfigurationError) as caught:
                    check.validate_environment()
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(url, str(caught.exception))

    def test_numeric_startup_values_do_not_become_shell_or_log_inputs(self):
        for name, value in [('PORT', '8000;echo fixture-secret'), ('PORT', '65536'),
                            ('OPERATIONS_STARTUP_WAIT_SECONDS', '-1'), ('MAX_BODY_BYTES', 'NaN')]:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaises(check.HostedConfigurationError) as caught:
                    check.validate_environment()
                self.assertNotIn(value, str(caught.exception))

    def test_backup_probe_is_private_and_leaves_no_file(self):
        check.check_backup_directory(self.backup)
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.backup.iterdir()), [])
        self.backup.chmod(0o755)
        with self.assertRaisesRegex(check.HostedConfigurationError, '0700'):
            check.check_backup_directory(self.backup)

    def test_backup_path_must_be_absolute(self):
        with patch.dict(os.environ, {'OPERATIONS_BACKUP_DIR': 'local-backups'}):
            with self.assertRaises(check.HostedConfigurationError):
                check.validate_environment()

    def database(self, row=(True, True, False, 'off'), version=170006):
        conn = SimpleNamespace(info=SimpleNamespace(server_version=version))
        cursor = SimpleNamespace(fetchone=lambda: row)
        conn.execute = lambda *args, **kwargs: cursor
        context = unittest.mock.MagicMock()
        context.__enter__.return_value = conn
        return context

    def test_checks_actual_server_major_and_required_privileges(self):
        with patch('psycopg.connect', return_value=self.database(version=170006)), \
             patch('umbrella.backup._toolchain') as tools:
            self.assertEqual(check.check_database(), 17)
            tools.assert_called_once_with(17)
        for row in [(False, True, False, 'off'), (True, False, False, 'off'),
                    (True, True, True, 'off'), (True, True, False, 'on')]:
            with self.subTest(row=row), patch('psycopg.connect', return_value=self.database(row)), \
                 patch('umbrella.backup._toolchain') as tools:
                with self.assertRaises(check.HostedConfigurationError):
                    check.check_database()
                tools.assert_not_called()

    def test_missing_matching_tools_refuses_startup(self):
        from umbrella.backup import BackupToolError
        with patch('psycopg.connect', return_value=self.database()), \
             patch('umbrella.backup._toolchain', side_effect=BackupToolError('No matching PostgreSQL 17 pair')):
            with self.assertRaisesRegex(check.HostedConfigurationError, 'PostgreSQL 17'):
                check.check_database()

    def test_connection_errors_are_redacted(self):
        import psycopg
        with patch('psycopg.connect', side_effect=psycopg.OperationalError('fixture-password postgresql://secret')):
            with self.assertRaises(check.DatabaseUnavailable) as caught:
                check.check_database()
        self.assertNotIn('fixture-password', str(caught.exception))
        self.assertNotIn('postgresql://secret', str(caught.exception))

    def test_start_executes_single_worker_only_after_preflight(self):
        settings = {'backup_directory': self.backup, 'wait_seconds': 0, 'port': 9123}
        with patch.object(start, 'validate_environment', return_value=settings), \
             patch.object(start, 'check_backup_directory') as backups, \
             patch.object(start, 'check_database', return_value=17), \
             patch.object(start.os, 'chdir'), patch.object(start.os, 'execv') as execute:
            start.main()
        backups.assert_called_once_with(self.backup)
        args = execute.call_args.args[1]
        self.assertEqual(args[args.index('--port') + 1], '9123')
        self.assertEqual(args[args.index('--workers') + 1], '1')
        self.assertIn('--no-proxy-headers', args)

    def test_failed_database_check_never_executes_server(self):
        settings = {'backup_directory': self.backup, 'wait_seconds': 0, 'port': 8000}
        with patch.object(start, 'validate_environment', return_value=settings), \
             patch.object(start, 'check_backup_directory'), \
             patch.object(start, 'check_database', side_effect=check.DatabaseUnavailable('Database is not ready')), \
             patch.object(start.os, 'execv') as execute, redirect_stderr(StringIO()):
            self.assertEqual(start.main(), 1)
        execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
