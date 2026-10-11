# Local output checks and atomic approval decisions

Baseline: PR10 commit9778aea. Follow-up adds a local schema boundary and compare-and-set approval claims. Offline only; all model/delivery calls mocked. No real messages sent, deployment or UI changes.

## Changes

- Extraction must be a JSON object with exactly the pack's extraction fields plus action_type, all strings. action_type must be declared and implemented. Provider schemas now specify additionalProperties=false. Local checks still apply if the provider ignores structured output.
- Empty, whitespace, non-text and >100000-character drafts cannot reach an approval policy. Invalid extraction is rejected before the second call; invalid drafts roll back under existing route/inbox transactions.
- Human approve/reject and policy auto-approval claim pending actions through one conditional UPDATE. Concurrent readers cannot both win. Auto-approval cannot resurrect sent/rejected actions.

## Limits

Schema validation does not prove extraction truth, prevent prompt injection, or fact-check drafts. Empty strings remain allowed for unknown extracted facts because packs declare only string types. Extra fields are intentionally rejected. The draft size cap may reject oversized outputs that used to be stored.

Approval delivery is still simulated. Atomic claims prevent concurrent decision duplicates within SQLite, not exactly-once real external delivery. A send failure/commit failure needs a real outbox before production send is added. Shared API-key authorization remains unchanged; no per-user authorization added. Existing pending workflows and human-decision timestamp semantics remain intact.

## Reproduction

Run from repo root:

    python -m pytest operations_nerd/tests/test_output_boundary.py operations_nerd/tests/test_decision_races.py operations_nerd/tests/test_durable_inbox.py -q

Old standalone scripts must run from operations_nerd/, which owns the frontend relative path. The initial root-directory attempt stopped at test_auth_and_packs on that path; rerun from the supported directory passed all12 scripts. Live provider smoke excluded.

## Actual before output

```text
FFFFF....FFFFF.FFFFF                                                     [100%]
=================================== FAILURES ===================================
________ test_invalid_extraction_fails_before_draft_or_writes[missing] _________
operations_nerd/tests/test_output_boundary.py:38: in test_invalid_extraction_fails_before_draft_or_writes
    process_event(conn, load_pack("realestate"), eid)
operations_nerd/pipeline/service.py:117: in process_event
    draft_prompt = render_template(pack.prompts[action_type], draft_context)
operations_nerd/pipeline/templating.py:35: in render_template
    return _REF.sub(replace, template)
operations_nerd/pipeline/templating.py:28: in replace
    raise TemplateRenderError(
E   pipeline.templating.TemplateRenderError: Template references {{event.parsed_data.listing_ref}}, but 'listing_ref' was not found in the provided context. This usually means a prompt references data the pipeline never supplied, or a pack references a field that doesn't exist.
_______ test_invalid_extraction_fails_before_draft_or_writes[list-field] _______
operations_nerd/tests/test_output_boundary.py:36: in test_invalid_extraction_fails_before_draft_or_writes
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
_______ test_invalid_extraction_fails_before_draft_or_writes[null-field] _______
operations_nerd/tests/test_output_boundary.py:36: in test_invalid_extraction_fails_before_draft_or_writes
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
______ test_invalid_extraction_fails_before_draft_or_writes[number-field] ______
operations_nerd/tests/test_output_boundary.py:36: in test_invalid_extraction_fails_before_draft_or_writes
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
______ test_invalid_extraction_fails_before_draft_or_writes[extra-field] _______
operations_nerd/tests/test_output_boundary.py:36: in test_invalid_extraction_fails_before_draft_or_writes
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
_____________ test_invalid_draft_never_enters_auto_approval[empty] _____________
operations_nerd/tests/test_output_boundary.py:51: in test_invalid_draft_never_enters_auto_approval
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
__________ test_invalid_draft_never_enters_auto_approval[whitespace] ___________
operations_nerd/tests/test_output_boundary.py:51: in test_invalid_draft_never_enters_auto_approval
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
_____________ test_invalid_draft_never_enters_auto_approval[null] ______________
operations_nerd/tests/test_output_boundary.py:51: in test_invalid_draft_never_enters_auto_approval
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
____________ test_invalid_draft_never_enters_auto_approval[object] _____________
operations_nerd/tests/test_output_boundary.py:51: in test_invalid_draft_never_enters_auto_approval
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
___________ test_invalid_draft_never_enters_auto_approval[oversized] ___________
operations_nerd/tests/test_output_boundary.py:51: in test_invalid_draft_never_enters_auto_approval
    with pytest.raises(PipelineError):
E   Failed: DID NOT RAISE PipelineError
_________________ test_invalid_inbox_attempt_can_retry_cleanly _________________
operations_nerd/tests/test_output_boundary.py:75: in test_invalid_inbox_attempt_can_retry_cleanly
    assert failed["status"] == "failed" and failed["last_error"] == "PipelineError"
E   AssertionError: assert ('failed' == 'failed'
E     
E       failed and 'TemplateRenderError' == 'PipelineError'
E     
E     - PipelineError
E     + TemplateRenderError)
________________ test_only_one_decision_wins[approve_and_send] _________________
operations_nerd/tests/test_decision_races.py:48: in test_only_one_decision_wins
    assert results.count("won") == 1
E   AssertionError: assert 2 == 1
E    +  where 2 = <built-in method count of list object at 0x7f3345073c40>('won')
E    +    where <built-in method count of list object at 0x7f3345073c40> = ['won', 'won'].count
__________________ test_only_one_decision_wins[reject_action] __________________
operations_nerd/tests/test_decision_races.py:48: in test_only_one_decision_wins
    assert results.count("won") == 1
E   AssertionError: assert 2 == 1
E    +  where 2 = <built-in method count of list object at 0x7f3344f58c00>('won')
E    +    where <built-in method count of list object at 0x7f3344f58c00> = ['won', 'won'].count
_______________ test_only_one_decision_wins[maybe_auto_approve] ________________
operations_nerd/tests/test_decision_races.py:48: in test_only_one_decision_wins
    assert results.count("won") == 1
E   AssertionError: assert 2 == 1
E    +  where 2 = <built-in method count of list object at 0x7f33450edc40>('won')
E    +    where <built-in method count of list object at 0x7f33450edc40> = ['won', 'won'].count
_________________ test_auto_policy_never_sends_rejected_action _________________
operations_nerd/tests/test_decision_races.py:61: in test_auto_policy_never_sends_rejected_action
    assert maybe_auto_approve(conn, aid) is None
E   AssertionError: assert {'id': 1, 'event_id': 1, 'business_id': 1, 'contact_id': None, ...} is None
E    +  where {'id': 1, 'event_id': 1, 'business_id': 1, 'contact_id': None, ...} = maybe_auto_approve(<sqlite3.Connection object at 0x7f334527ba40>, 1)
=========================== short test summary info ============================
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_extraction_fails_before_draft_or_writes[missing]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_extraction_fails_before_draft_or_writes[list-field]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_extraction_fails_before_draft_or_writes[null-field]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_extraction_fails_before_draft_or_writes[number-field]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_extraction_fails_before_draft_or_writes[extra-field]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_draft_never_enters_auto_approval[empty]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_draft_never_enters_auto_approval[whitespace]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_draft_never_enters_auto_approval[null]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_draft_never_enters_auto_approval[object]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_draft_never_enters_auto_approval[oversized]
FAILED operations_nerd/tests/test_output_boundary.py::test_invalid_inbox_attempt_can_retry_cleanly
FAILED operations_nerd/tests/test_decision_races.py::test_only_one_decision_wins[approve_and_send]
FAILED operations_nerd/tests/test_decision_races.py::test_only_one_decision_wins[reject_action]
FAILED operations_nerd/tests/test_decision_races.py::test_only_one_decision_wins[maybe_auto_approve]
FAILED operations_nerd/tests/test_decision_races.py::test_auto_policy_never_sends_rejected_action
15 failed, 5 passed in 0.99s
```

## Actual after output

```text
...............................                                          [100%]
=============================== warnings summary ===============================
operations_nerd/tests/test_durable_inbox.py::test_api_enqueue_conflict_process_and_key_gate
  /tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
31 passed, 1 warning in 1.63s
```

## Existing regression output (12 scripts)

```text
=== test_approval_smoke ===
Testing approve_and_send on a pending action...
  Approved -> status: sent
  Correctly raised on double-approve: Drafted action 1 is not pending approval (current status: sent)
Scenario (a) passed.

Testing reject_action on a pending action...
  Rejected -> status: rejected
  Correctly raised on acting on non-pending action: Drafted action 2 is not pending approval (current status: rejected)
Scenario (b) passed.

Testing auto_approve policy...
  process_event with auto_approve policy -> status: sent
  Confirmed not in pending list: []
Scenario (c) passed.

Approval smoke test passed.
=== test_atomicity_smoke ===
Testing that a failed process_event rolls back the whole transaction...
  Pre-state OK: event unprocessed, no drafted actions
  PipelineError raised as expected: Pack 'realestate' has no event schema for source 'not_a_real_source'
  Post-state OK: failed event unprocessed, no drafted actions

Testing rollback when failure happens AFTER the first write...
  RuntimeError raised as expected: simulated LLM outage during drafting
  ATOMICITY PROVEN: failure mid-pipeline left zero writes in the DB

Atomicity smoke test passed.
=== test_auth_and_packs ===
/tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
auth+packs OK
=== test_db_smoke ===
Created business #1
Created contact #1
Created follow-up #1
Created + processed event #1
Created drafted action #1
Auto-approve for send_availability_reply: False
Pending actions: 1
{'id': 1, 'event_id': 1, 'business_id': 1, 'contact_id': 1, 'action_type': 'send_availability_reply', 'payload': {'subject': 'Re: 5th and Main', 'body': "Yes, it's still available! Want to schedule a showing?"}, 'status': 'pending_approval', 'created_at': '2026-10-11 06:14:31', 'decided_at': None}
Contacts for business: 1
{'id': 1, 'business_id': 1, 'name': 'Jordan Lee', 'email': 'jordan@example.com', 'phone': '555-0101', 'status': 'active', 'created_at': '2026-10-11 06:14:31', 'updated_at': '2026-10-11 06:14:31', 'extension_data_json': '{"budget_max":"650000","preapproved":"true","property_type":"\\"condo\\""}', 'extension_data': {'budget_max': 650000, 'preapproved': True, 'property_type': 'condo'}}
Contact IDs with property_type=condo: [1]

Smoke test passed.
=== test_decided_at_smoke ===
Testing that human approve_and_send sets decided_at...
  Pre-approve: status=pending_approval, decided_at=None
  Post-approve: status=sent, decided_at=2026-10-11 06:14:31
Scenario (a) passed.

Testing that human reject_action sets decided_at...
  Post-reject: status=rejected, decided_at=2026-10-11 06:14:31
Scenario (b) passed.

Testing that maybe_auto_approve leaves decided_at NULL...
  Post-auto: status=sent, decided_at=None
  -> decided_at remains NULL because no human made a decision.
Scenario (c) passed.

decided_at semantic smoke test passed.
=== test_insights ===
/tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
insights OK
=== test_intake ===
/tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
intake OK
=== test_list_no_n_plus_1 ===
Seeding 20 contacts with 3 EAV attrs each...
  Seeded business #1 with 20 contacts + 10 follow-ups

Testing list_contacts query count...
  Queries issued: 1 (expected: 1, would be 21 with N+1)
  Returned 20 contacts, sample extension_data: {'budget_max': 500000, 'preapproved': True, 'property_type': 'condo'}

Testing list_follow_ups query count...
  Queries issued: 1 (expected: 1, would be 11 with N+1)
  Returned 10 follow-ups, sample extension_data: {'listing_ids': ['L-1', 'L-2']}

Testing list_follow_ups(status='pending') query count...
  Queries issued: 1 (expected: 1)
  Returned 10 pending follow-ups, all with correct extension_data

N+1 elimination smoke test passed.
=== test_llm_providers ===
Testing mock provider...
  OK
Testing unknown provider...
  OK
Testing retry on transient errors...
  OK
Testing openai request shape...
  OK
LLM provider smoke test passed.
=== test_pipeline_smoke ===
Testing render_template...
  OK: Office hours are 9-6. Email: Hi there
  Correctly raised on missing ref: Template references {{settings.nonexistent}}, but 'nonexistent' was not found in the provided context. This usually means a prompt references data the pipeline never supplied, or a pack references a field that doesn't exist.

Testing _build_extraction_schema...
  {'type': 'object', 'properties': {'intent': {'type': 'string'}, 'urgency': {'type': 'string'}, 'action_type': {'type': 'string', 'enum': ['send_availability_reply', 'escalate_to_agent']}}, 'required': ['intent', 'urgency', 'action_type'], 'additionalProperties': False}

Testing process_event with mocked LLM...
  Result: {'id': 1, 'action_type': 'send_availability_reply', 'payload': {'body': "Yes, it's still available! Want to schedule a showing?"}, 'parsed_data': {'intent': 'availability_check', 'listing_ref': '5th and Main', 'urgency': 'high', 'action_type': 'send_availability_reply'}, 'status': 'pending_approval'}
  Event marked processed with parsed_data: {'intent': 'availability_check', 'listing_ref': '5th and Main', 'urgency': 'high', 'action_type': 'send_availability_reply'}
  Drafted action in DB: {'id': 1, 'event_id': 1, 'business_id': 1, 'contact_id': None, 'action_type': 'send_availability_reply', 'payload': {'body': "Yes, it's still available! Want to schedule a showing?"}, 'status': 'pending_approval', 'created_at': '2026-10-11 06:14:33', 'decided_at': None}

Pipeline smoke test (mocked LLM) passed.
=== test_questionnaire_api_smoke ===
/tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
GET /packs/realestate/questionnaire -> 200
  Questions returned: ['office_hours', 'default_follow_up_days', 'property_types']
GET /packs/nonexistent/questionnaire -> 404
POST /businesses (valid) -> 200
  Response: {'business_id': 1, 'industry_pack': 'realestate', 'settings': {'office_hours': '9-6', 'default_follow_up_days': 2, 'property_types': ['condo', 'single_family']}}
  Business in DB: {'id': 1, 'name': 'Riverbend Realty', 'industry_pack': 'realestate', 'pack_version': '0.1.0', 'settings_json': {'office_hours': '9-6', 'default_follow_up_days': 2, 'property_types': ['condo', 'single_family']}, 'created_at': '2026-10-11 06:14:34'}
POST /businesses (invalid) -> 422
  Issues: ['default_follow_up_days: expected a number, got str', "property_types: invalid option(s) ['yacht'], allowed values are ['commercial', 'condo', 'multi_family', 'single_family']"]
POST /businesses (select as list) -> 422
  Issues: ['preferred_language: expected text, got list']
POST /businesses (select as object) -> 422
  Issues: ['preferred_language: expected text, got dict']

Questionnaire API smoke test passed.
=== test_xss_in_frontend ===
/tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient as TestClient  # noqa
Testing that the loader accepts a pack with HTML in display_name...
  display_name: 'Evil Pack </option><img src=x onerror="window.__pwned=true">'

Testing that /packs returns the string verbatim to the frontend...
  /packs returns: {'id': 'evil', 'display_name': 'Evil Pack </option><img src=x onerror="window.__pwned=true">'}
  /packs API response: ['healthclub', 'realestate']

Threat model documented:
  - Loader passes display_name verbatim (no escaping).
  - JSON wire format preserves the string as-is.
  - XSS prevention lives in the frontend's createElement/textContent
    path. Verifying the actual DOM behavior requires a browser test;
    the static check is that the JS no longer uses innerHTML for
    these two render sites (lines 137-149 and 240-256 of the new
    frontend/index.html).
  Static check: 'innerHTML' is no longer used in frontend/index.html. PASS

XSS frontend safety smoke test passed.
```
