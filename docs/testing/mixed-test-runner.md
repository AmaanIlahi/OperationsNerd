# Running the mixed test suite

Create a virtual environment with Python 3.11 (the CI version), then install
`pip install -r requirements-dev.txt`. From the repository root run:

```sh
LLM_PROVIDER=stub python scripts/run_tests.py
python scripts/tests/test_runner.py
python -m pytest -q modules/accounting/tests
```

The suite contains both standalone scripts and pytest files. Running a pytest
file as `python test_file.py` only defines its functions and fixtures. A zero
exit code from that command does not mean its test cases ran.

The runner inspects each selected file's syntax tree without importing it. Files
that import pytest run in a child process with `python -m pytest -q`; other files
keep the standalone Python execution path. Comments and string literals do not
change the selected runner. A pytest file with no collected tests is a failure,
not a successful empty run. Syntax errors and child failures stay failures.

The runner preserves the core scripts' `operations_nerd` working directory and
the modules' repository-root working directory. Tests in `scripts/tests` are
included, and existing substring filters still apply. The live LLM smoke test
remains excluded. Pytest is a development/test dependency, not a runtime one.

This is intentionally an import-based adapter for this repository's existing
mixed convention. A pytest test that only receives pytest fixtures indirectly,
without importing pytest itself, needs an explicit pytest import or a direct
pytest invocation. This is not a general pytest collector replacement.

## Regression coverage

The runner's own offline suite checks imports and aliases, comments and strings,
fixtures and parameterization, collection failures, standalone `__main__`
execution, nonzero exit codes, timeouts, filters/discovery, working directories,
and the live-smoke exclusion. It does not call a model or external provider.
