"""Run core and module tests, preserving standalone scripts and pytest fixtures.

Standalone scripts run in their own process. Files that import pytest run via
``python -m pytest`` so fixtures and parametrized cases are actually executed.
Core scripts run from the operations_nerd folder; module and runner tests run
from the repo root. test_llm_smoke.py is skipped because it uses model credits.

    python scripts/run_tests.py                    # run all
    python scripts/run_tests.py phase_a            # only files whose name contains "phase_a"
    python scripts/run_tests.py modules/hr         # only the HR module's tests
    python scripts/run_tests.py modules/accounting # only the accounting module's tests

Exit code is 0 only when every selected script exits 0.
"""

import ast
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "operations_nerd"
MODULES_DIR = ROOT / "modules"
SKIPPED = {"test_llm_smoke.py"}
TIMEOUT_SECONDS = 300


def select(filters):
    files = sorted((APP_DIR / "tests").glob("test_*.py")) + sorted(MODULES_DIR.glob("*/tests/test_*.py"))
    files += sorted((ROOT / "scripts" / "tests").glob("test_*.py"))
    files = [f for f in files if f.name not in SKIPPED]
    if filters:
        files = [f for f in files if any(part in label(f) for part in filters)]
    return files


def label(path):
    """test_x.py for the core tests, modules/<name>/tests/test_x.py for a module's."""
    return path.name if path.is_relative_to(APP_DIR) else path.relative_to(ROOT).as_posix()


def command(path):
    """Choose pytest only for a real import, never text in comments or strings.

    Do not import test modules here: collection must stay in the child process.
    Syntax errors are left to Python so they fail with a useful diagnostic.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeError):
        return [sys.executable, str(path)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            name.name == "pytest" or name.name.startswith("pytest.") for name in node.names
        ):
            return [sys.executable, "-m", "pytest", "-q", str(path)]
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == "pytest" or node.module.startswith("pytest.")
        ):
            return [sys.executable, "-m", "pytest", "-q", str(path)]
    return [sys.executable, str(path)]


def run_one(path):
    started = time.monotonic()
    try:
        done = subprocess.run(
            command(path),
            cwd=APP_DIR if path.is_relative_to(APP_DIR) else ROOT,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        code, output = done.returncode, (done.stdout + done.stderr)
    except subprocess.TimeoutExpired as exc:
        code = 124
        output = f"timed out after {TIMEOUT_SECONDS}s\n{exc.stdout or ''}"
    return code, output, time.monotonic() - started


def main(argv):
    files = select(argv)
    if not files:
        print("no test scripts matched", file=sys.stderr)
        return 2
    failed = []
    for path in files:
        code, output, seconds = run_one(path)
        status = "ok  " if code == 0 else "FAIL"
        print(f"{status} {label(path)} ({seconds:.1f}s)")
        if code != 0:
            failed.append(label(path))
            print(output.rstrip()[-2000:])
    print(f"\n{len(files) - len(failed)} of {len(files)} test scripts passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
