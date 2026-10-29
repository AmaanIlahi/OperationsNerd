# Contributing

Read `documentation/agentic_crm_architecture.md` first. It is short and sets the rules the code follows.

## Run it locally

1. Install Python 3.11 and clone the repo.
2. From the repo root:
   ```
   python -m venv venv
   venv\Scripts\activate          # Mac/Linux: source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` (repo root, never committed) and set:
   ```
   GEMINI_API_KEY=your-key            # from aistudio.google.com
   SIGNUP_INVITE_CODE=any-string      # you type this on the sign-up screen
   ```
4. Start the server **from the `operations_nerd` folder** (the app looks for `./frontend` there):
   ```
   cd operations_nerd
   uvicorn main:app --reload
   ```
5. Open http://127.0.0.1:8000/app/ , sign up with your invite code, create a business, chat.

The old demo (`/ui`) also runs locally. It is switched off when `APP_ENV=production`.

## Tests

From `operations_nerd/`, each test file is a plain script (no pytest needed):

```
python tests/test_agentic_phase_a.py
```

Run them all except `test_llm_smoke.py`, which calls Gemini for real and uses credits:

```
for f in tests/test_*.py; do [ "$f" = tests/test_llm_smoke.py ] || python $f; done        # bash
Get-ChildItem tests\test_*.py | Where Name -ne test_llm_smoke.py | % { python $_.FullName }   # PowerShell
```

Or run them all with one command from the repo root (it skips `test_llm_smoke.py` and prints a summary; add a name fragment to run a subset, for example `python scripts/run_tests.py phase_a`):

```
python scripts/run_tests.py
```

It also runs each standalone module's tests (`modules/<name>/tests/test_*.py`) from the repo root; a path
fragment selects one module, for example `python scripts/run_tests.py modules/hr`.

The same command runs on every pull request in GitHub Actions (`.github/workflows/tests.yml`).

All should pass before you open a PR. Tests use a stub model, so they are free and need no key.

## Benchmark

From the repo root. It runs the task suite against the real API and records time, tokens, cost and data changes.

```
python scripts/benchmark/run_benchmark.py --dry-run --runs 1   # scripted stub, no key, a few seconds
python scripts/benchmark/run_benchmark.py --runs 3             # live Gemini: ~13 minutes, ~$0.30
```

Output goes to `scripts/benchmark/results/<timestamp>/report.md`. Live full runs are also logged in
`scripts/benchmark/results/HISTORY.md`. Add `--commit` only when a maintainer asks you to log a run.

## Branches and pull requests

- Branch off `main`: `git checkout main && git pull && git checkout -b yourname/short-topic`.
- Push your branch and open a pull request into `main`. **No direct pushes to `main`.**
- Keep PRs small and about one thing. Include tests with the code.
- Do not mix formatting changes with behaviour changes.

## What not to touch

- The legacy app: `/ui`, `operations_nerd/questionnaire/`, `operations_nerd/pipeline/`, `operations_nerd/frontend/index.html`.
- The agent turn, the validator and the operation set (`operations_nerd/agentic/`) unless it is agreed first.
  The agent never writes code; it only proposes changes that are validated and approved.
- `main.py`: additive changes only.

## Where new work goes

- **Logistics rules:** `modules/logistics/`, as standalone code with its own tests. No imports from the core yet;
  the module interface is not built. Keep rules generic to logistics, never per company.
- **HR (employee records and leave):** `modules/hr/`, standalone like logistics, with versioned migrations in
  `modules/hr/migrations/`. No payroll or other sensitive fields until encryption and access control exist
  (see `modules/hr/README.md`).
- **Core data integrity:** the db layer, `operations_nerd/db/`, with tests in `operations_nerd/tests/`.
