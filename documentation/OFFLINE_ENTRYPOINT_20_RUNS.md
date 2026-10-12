# Offline test entrypoint

Before: root pytest discovers a live-model smoke module and fails collection on frontend relative paths (6 errors). After: scripts/test_offline.py selects the supported app directory and explicitly excludes the live-model module. No model call/credit use. The standalone smoke script remains available separately; this is not live-provider verification.

Run: `python scripts/test_offline.py`.

| Run | Working directory | Result |
|---|---|---|
| 1 | repository root | 39 passed, 1 warning in 2.80s |
| 2 | unrelated directory | 39 passed, 1 warning in 2.98s |
| 3 | repository root | 39 passed, 1 warning in 2.54s |
| 4 | unrelated directory | 39 passed, 1 warning in 2.99s |
| 5 | repository root | 39 passed, 1 warning in 3.06s |
| 6 | unrelated directory | 39 passed, 1 warning in 2.97s |
| 7 | repository root | 39 passed, 1 warning in 3.41s |
| 8 | unrelated directory | 39 passed, 1 warning in 3.11s |
| 9 | repository root | 39 passed, 1 warning in 3.08s |
| 10 | unrelated directory | 39 passed, 1 warning in 2.87s |
| 11 | repository root | 39 passed, 1 warning in 2.97s |
| 12 | unrelated directory | 39 passed, 1 warning in 2.88s |
| 13 | repository root | 39 passed, 1 warning in 2.71s |
| 14 | unrelated directory | 39 passed, 1 warning in 2.99s |
| 15 | repository root | 39 passed, 1 warning in 2.91s |
| 16 | unrelated directory | 39 passed, 1 warning in 2.86s |
| 17 | repository root | 39 passed, 1 warning in 2.62s |
| 18 | unrelated directory | 39 passed, 1 warning in 2.90s |
| 19 | repository root | 39 passed, 1 warning in 2.95s |
| 20 | unrelated directory | 39 passed, 1 warning in 2.86s |

20/20 runs passed,39 pytest tests per run. No claim that excluded provider smoke passed.
