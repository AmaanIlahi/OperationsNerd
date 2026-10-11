# Durable event inbox

This adds owner-triggered durable intake on top of PR10. It does not replace Amaan's agentic CRM/spec work or the existing signed webhook.

## Before / after

BEFORE existing webhook, same delivery twice: 2 events; 2 drafts; 4 model calls
AFTER durable inbox, same delivery twice: receipt IDs 1 1 ; completed ; 1 attempt; 2 model calls
AFTER action status: pending_approval

The above is a controlled offline fixture with the real DB and routes, a mocked LLM, and two deliveries of identical content. It demonstrates duplicate prevention, not live provider performance.

## API

Use the existing X-API-Key header when OPERATIONS_API_KEY is configured. Enable that key before exposing these routes to a network. Existing local no-key behavior is unchanged.

1. POST /businesses/{business_id}/inbox with source, idempotency_key, raw_content. Returns 202 with a durable receipt. No LLM call yet.
2. Reuse the same key for delivery retries. Same content returns the same receipt. Different content returns 409. Key scope is business + source.
3. GET /businesses/{business_id}/inbox?status=queued (or failed/completed) inspects the queue and attempt/error metadata.
4. POST /businesses/{business_id}/inbox/{receipt_id}/process performs one attempt. Completed receipts replay without LLM calls. Failed receipts remain durable and can be retried through this same endpoint.

Processing creates a normal event/draft and preserves the existing human approval policy. No new sender, connector, background worker or external send is added. Existing approval policies still apply. A failed attempt rolls event/draft writes back and records only the exception class, not provider errors/prompts/secrets.

## Integration limits

- Existing /webhooks routes and /events routes remain synchronous and do not become idempotent automatically. Integrations must opt into the inbox API with stable delivery IDs.
- No scheduled worker, backoff, dead-letter automation, claim lease, admin UI or server deployment is included.
- Processing reserves SQLite's writer lock during model calls. This serializes duplicate attempts but is low-throughput; contention can hit SQLite's lock timeout. Use a dedicated worker/short claim transaction design before scaling.
- Crash recovery relies on SQLite transaction rollback; provider calls may have happened before a crash, so retries can incur another model call. The guarantee here is one committed draft per completed receipt, not exactly-once calls or external effects.
- Business IDs scope data but are not individual user authorization. The existing shared API key is the current access model.
- init_db applies additive CREATE TABLE IF NOT EXISTS schema. Existing records are preserved. No destructive migration.

## Test output

```text
...........                                                              [100%]
=============================== warnings summary ===============================
operations_nerd/tests/test_durable_inbox.py::test_api_enqueue_conflict_process_and_key_gate
  /tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
11 passed, 1 warning in 0.98s
```

11 deterministic integration tests cover queue-only intake, duplicate/conflict handling, single draft/replay, failed attempt rollback/retry, business isolation, source validation, additive migration, concurrent processing and HTTP/API-key behavior. Mock provider only; no API spend.

12 existing offline test scripts passed: approval, atomicity, DB, decided_at, pipeline, questionnaire API, intake, auth/pack validation, insights, LLM-provider mocks, list query counts and static frontend escaping. Live test_llm_smoke.py intentionally not run. Output is attached separately in DURABLE_INBOX_EXISTING_RESULTS.txt. No UI changed; no screenshot claim.

Run:
```bash
PYTHONPATH=operations_nerd python -m pytest -q operations_nerd/tests/test_durable_inbox.py
```
