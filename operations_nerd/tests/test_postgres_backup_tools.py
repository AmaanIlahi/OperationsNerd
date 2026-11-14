"""Backup tooling must match the server, regardless of ambient PATH order."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from umbrella import backup


class BackupToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='opsnerd-tool-test-')
        self.root = Path(self.directory.name).resolve()
        self.environment = patch.dict(os.environ, {'OPERATIONS_PG_BIN': '', 'PATH': ''})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def tools(self, folder, dump_major, restore_major=None):
        directory = self.root / folder
        directory.mkdir()
        for name, major in [('pg_dump', dump_major), ('pg_restore', restore_major if restore_major is not None else dump_major)]:
            path = directory / name
            path.write_text(f'#!/bin/sh\necho "{name} (PostgreSQL) {major}.2"\n')
            path.chmod(0o700)
        return directory

    def test_bundled_pair_wins_over_older_path_without_running_old_tool(self):
        old = self.tools('system14', 14)
        bundled = self.tools('bundled16', 16)
        marker = self.root / 'old-tool-was-executed'
        (old / 'pg_dump').write_text(f'#!/bin/sh\n: > "{marker}"\necho "pg_dump (PostgreSQL) 14.2"\n')
        with patch.dict(os.environ, {'PATH': str(old)}), patch.object(backup, '_bundled_bin', return_value=bundled):
            self.assertEqual(backup._toolchain(16), (str(bundled / 'pg_dump'), str(bundled / 'pg_restore')))
        self.assertFalse(marker.exists())

    def test_matching_later_path_pair_selected_when_bundle_is_older(self):
        bundled = self.tools('bundled16', 16)
        old = self.tools('system14', 14)
        current = self.tools('server17', 17)
        with patch.dict(os.environ, {'PATH': os.pathsep.join([str(old), str(current)])}), \
             patch.object(backup, '_bundled_bin', return_value=bundled):
            self.assertEqual(backup._toolchain(17), (str(current / 'pg_dump'), str(current / 'pg_restore')))
        with patch.dict(os.environ, {'PATH': str(current)}), patch.object(backup, '_bundled_bin', return_value=None):
            self.assertEqual(backup._toolchain(17), (str(current / 'pg_dump'), str(current / 'pg_restore')))

    def test_pair_must_share_directory_and_server_major(self):
        mixed = self.tools('mixed', 16, 14)
        newer = self.tools('newer', 17)
        (newer / 'pg_restore').unlink()
        with patch.dict(os.environ, {'PATH': str(mixed)}), patch.object(backup, '_bundled_bin', return_value=newer):
            with self.assertRaisesRegex(backup.BackupToolError, 'PostgreSQL 16.*OPERATIONS_PG_BIN'):
                backup._toolchain(16)

    def test_explicit_directory_with_spaces_is_authoritative(self):
        selected = self.tools('operator tools', 16)
        bundled = self.tools('bundled', 16)
        with patch.dict(os.environ, {'OPERATIONS_PG_BIN': str(selected)}), \
             patch.object(backup, '_bundled_bin', return_value=bundled):
            self.assertEqual(backup._toolchain(16), (str(selected / 'pg_dump'), str(selected / 'pg_restore')))
            with self.assertRaisesRegex(backup.BackupToolError, 'PostgreSQL 17.*OPERATIONS_PG_BIN'):
                backup._toolchain(17)

    def test_missing_or_failed_version_probes_stop_safely(self):
        tools = self.tools('broken', 16)
        for failure in (subprocess.TimeoutExpired('pg_dump', 5), OSError('fixture unavailable')):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(backup, '_bundled_bin', return_value=tools), \
                 patch.object(backup.subprocess, 'run', side_effect=failure):
                with self.assertRaises(backup.BackupToolError):
                    backup._toolchain(16)
        with patch.object(backup, '_bundled_bin', return_value=tools), \
             patch.object(backup.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'fixture failure')):
            with self.assertRaises(backup.BackupToolError):
                backup._toolchain(16)
        (tools / 'pg_dump').write_text('#!/bin/sh\necho "unexpected client version"\n')
        with patch.object(backup, '_bundled_bin', return_value=tools):
            with self.assertRaises(backup.BackupToolError):
                backup._toolchain(16)

    def test_version_probes_do_not_inherit_model_or_database_credentials(self):
        tools = self.tools('verified', 16)
        captured = []
        def probe(args, **kwargs):
            captured.append(kwargs['env'])
            return subprocess.CompletedProcess(args, 0, f'{Path(args[0]).name} (PostgreSQL) 16.2\n', '')
        with patch.dict(os.environ, {'BASETEN_API_KEY': 'fixture-model-secret', 'PGPASSWORD': 'fixture-db-secret',
                                    'OPERATIONS_DATABASE_URL': 'postgresql://fixture-secret'}), \
             patch.object(backup, '_bundled_bin', return_value=tools), patch.object(backup.subprocess, 'run', side_effect=probe):
            backup._toolchain(16)
        self.assertEqual(len(captured), 2)
        for environment in captured:
            self.assertNotIn('BASETEN_API_KEY', environment)
            self.assertNotIn('PGPASSWORD', environment)
            self.assertNotIn('OPERATIONS_DATABASE_URL', environment)


if __name__ == '__main__':
    unittest.main()
