"""Offline test-runner regressions. Run directly or with unittest."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('nyu_runner', Path(__file__).resolve().parents[1] / 'run_tests.py')
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def file(self, text):
        p = self.root / 'test_example.py'
        p.write_text(text, encoding='utf-8')
        return p

    def test_plain_script(self):
        p = self.file('assert True\n')
        self.assertEqual(r.command(p), [sys.executable, str(p)])

    def test_import_pytest(self):
        p = self.file('import pytest\n')
        self.assertEqual(r.command(p), [sys.executable, '-m', 'pytest', '-q', str(p)])

    def test_aliased_import(self):
        p = self.file('import pytest as pt\n')
        self.assertIn('pytest', r.command(p))

    def test_from_import(self):
        p = self.file('from pytest import fixture\n')
        self.assertIn('-m', r.command(p))

    def test_nested_import(self):
        p = self.file('def f():\n    import pytest\n')
        self.assertIn('-m', r.command(p))

    def test_multiple_imports(self):
        p = self.file('import os, pytest, sys\n')
        self.assertIn('-m', r.command(p))

    def test_submodule_import(self):
        p = self.file('from pytest.mark import x\n')
        self.assertIn('-m', r.command(p))

    def test_comment_not_import(self):
        p = self.file('# import pytest\nassert True\n')
        self.assertNotIn('-m', r.command(p))

    def test_string_not_import(self):
        p = self.file('s = "import pytest"\n')
        self.assertNotIn('-m', r.command(p))

    def test_similar_module_not_pytest(self):
        p = self.file('import pytest_extra\n')
        self.assertNotIn('-m', r.command(p))

    def test_syntax_error_fails_in_child(self):
        p = self.file('def bad(:\n')
        code, out, _ = r.run_one(p)
        self.assertNotEqual(code, 0)
        self.assertIn('SyntaxError', out)

    def test_import_detection_has_no_side_effect(self):
        marker = self.root / 'marker'
        p = self.file(f'import pytest\nopen({str(marker)!r}, "w").write("bad")\n')
        r.command(p)
        self.assertFalse(marker.exists())

    def test_parametrized_failure_is_executed(self):
        p = self.file('import pytest\n@pytest.mark.parametrize("x", [1, 2, 3])\ndef test_case(x):\n    assert x != 2\n')
        code, out, _ = r.run_one(p)
        self.assertNotEqual(code, 0)
        self.assertIn('1 failed, 2 passed', out)

    def test_empty_pytest_suite_is_not_success(self):
        p = self.file('import pytest\n')
        code, out, _ = r.run_one(p)
        self.assertEqual(code, 5)
        self.assertIn('no tests ran', out)

    def test_plain_script_failure_is_retained(self):
        p = self.file('raise RuntimeError("expected-script-failure")\n')
        code, out, _ = r.run_one(p)
        self.assertNotEqual(code, 0)
        self.assertIn('expected-script-failure', out)

    def test_runner_regressions_are_discovered(self):
        self.assertIn(Path(__file__).resolve(), r.select(['scripts/tests']))

    def test_core_working_directory_preserved(self):
        p = r.APP_DIR / 'tests' / 'test_example.py'
        with patch.object(r, 'command', return_value=['python', 'example']), patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            r.run_one(p)
        self.assertEqual(run.call_args.kwargs['cwd'], r.APP_DIR)

    def test_fixture_is_resolved(self):
        p = self.file('import pytest\n@pytest.fixture\ndef value():\n    return 42\ndef test_value(value):\n    assert value == 42\n')
        code, out, _ = r.run_one(p)
        self.assertEqual(code, 0, out)
        self.assertIn('1 passed', out)

    def test_fixture_failure_fails_suite(self):
        p = self.file('import pytest\n@pytest.fixture\ndef value():\n    raise RuntimeError("fixture-failed")\ndef test_value(value):\n    pass\n')
        code, out, _ = r.run_one(p)
        self.assertNotEqual(code, 0)
        self.assertIn('fixture-failed', out)

    def test_plain_standalone_main_still_executes(self):
        p = self.file('if __name__ == "__main__":\n    print("standalone-executed")\n')
        code, out, _ = r.run_one(p)
        self.assertEqual(code, 0, out)
        self.assertIn('standalone-executed', out)

    def test_timeout_remains_failure(self):
        p = self.file('import time\ntime.sleep(10)\n')
        with patch.object(r, 'TIMEOUT_SECONDS', 0.05):
            code, out, _ = r.run_one(p)
        self.assertEqual(code, 124)
        self.assertIn('timed out', out)

    def test_no_selected_tests_remains_failure(self):
        with patch.object(r, 'select', return_value=[]):
            self.assertEqual(r.main([]), 2)

    def test_failed_child_fails_main(self):
        p = r.ROOT / 'modules' / 'example' / 'tests' / 'test_example.py'
        with patch.object(r, 'select', return_value=[p]), patch.object(r, 'run_one', return_value=(1, 'expected', 0.01)):
            self.assertEqual(r.main([]), 1)

    def test_skipped_live_llm_stays_excluded(self):
        self.assertFalse(any(p.name == 'test_llm_smoke.py' for p in r.select([])))

    def test_module_working_directory_preserved(self):
        p = r.ROOT / 'modules' / 'example' / 'tests' / 'test_example.py'
        with patch.object(r, 'command', return_value=['python', 'example']), patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            r.run_one(p)
        self.assertEqual(run.call_args.kwargs['cwd'], r.ROOT)


if __name__ == '__main__':
    unittest.main()
