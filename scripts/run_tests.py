"""Run every test script under operations_nerd/tests and report a summary.

The tests are plain scripts (no pytest). Each one is run in its own process
from the operations_nerd folder, the same way CONTRIBUTING.md describes.
test_llm_smoke.py is skipped because it calls a real model and uses credits.

    python scripts/run_tests.py            # run all
    python scripts/run_tests.py phase_a    # run only files whose name contains "phase_a"

Exit code is 0 only when every selected script exits 0.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "operations_nerd"
SKIPPED = {"test_llm_smoke.py"}
TIMEOUT_SECONDS = 300


def select(filters):
    files = sorted((APP_DIR / "tests").glob("test_*.py"))
    files = [f for f in files if f.name not in SKIPPED]
    if filters:
        files = [f for f in files if any(part in f.name for part in filters)]
    return files


def run_one(path):
    started = time.monotonic()
    try:
        done = subprocess.run(
            [sys.executable, str(path)],
            cwd=APP_DIR,
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
        print(f"{status} {path.name} ({seconds:.1f}s)")
        if code != 0:
            failed.append(path.name)
            print(output.rstrip()[-2000:])
    print(f"\n{len(files) - len(failed)} of {len(files)} test scripts passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
