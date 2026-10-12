"""Run offline tests from either repo root or another working directory.

The live provider smoke module is deliberately excluded: it performs model
calls at import time. Run it separately only with permission to use credits.
"""
from pathlib import Path
import subprocess
import sys

app = Path(__file__).resolve().parents[1] / 'operations_nerd'
raise SystemExit(subprocess.call(
    [sys.executable, '-m', 'pytest', '-q', 'tests', '--ignore=tests/test_llm_smoke.py'],
    cwd=app,
))
