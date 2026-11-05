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
- **Core data integrity:** the db layer, `operations_nerd/db/`, with tests in `operations_nerd/tests/`.

### Record search workspace

The CRM list supports literal, Unicode-normalized case-insensitive search over
active field values, and an exact select/boolean filter combined with that search.
The existing owner-only list endpoint accepts `q` (up to 200 characters) and
`equals` (a JSON object mapping active field keys to exact JSON values, up to
4000 encoded characters). Exact filters are case-sensitive and type-sensitive:
`true`, `1`, null and a missing value are different. A valid but wrong-type
filter yields no matches; unknown/archived field keys are rejected. Links and
archived fields are not searched. Matching happens before pagination.

This still scans the business's records in Python. It is intended for the
current small-business prototype, not a production-scale indexed search service.
A production release needs record-volume limits/indexing and endpoint abuse
controls. No schema, writes, model calls or extra external services are added.

Offline HTTP checks: `python operations_nerd/tests/test_record_search_api.py`.
Browser acceptance uses a disposable local SQLite database and synthetic data:
install Playwright/Chromium, then run
`python scripts/verify_record_search_browser.py`. It uses port 8131 and saves
screenshots under ignored `.browser-evidence/`. It never calls a live model.
