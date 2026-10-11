# OperationsNerd: three follow-up changes

1. Edited review claims pending atomically before writing its body/review or sending; concurrent ordinary approval cannot both win.
2. CSV intake validates unique normalized headers, strict parsing and row widths before writes; accepts at most10000 rows/5000000 characters. Existing per-row name/email rejection and dedupe remain.
3. Analytics counts auto approvals from distinct audited actions after final sent transition; sent-by-type counts only actual sent state.

Examples: python examples/review_intake_metrics.py. Full new+inbox boundary suite39pass;12existing offline scripts pass. Audit-derived auto counts cannot recover missing historic audit rows. Shared-key auth unchanged; simulated sending only.

All measured examples are offline and synthetic. No deploy/merge/release. Backend changes only, no screenshot claims.

## Regression before

```text
FFFFF.FF                                                                 [100%]
=================================== FAILURES ===================================
_________________ test_edit_race_preserves_only_winning_review _________________
operations_nerd/tests/test_review_intake_metrics.py:33: in test_edit_race_preserves_only_winning_review
    assert sum(x is not None for x in results)==1
E   assert 2 == 1
E    +  where 2 = sum(<generator object test_edit_race_preserves_only_winning_review.<locals>.<genexpr> at 0x7f9f83767300>)
_ test_malformed_csv_rejected_before_any_writes[name,email\nA,a@x.invalid,overflow\n] _
operations_nerd/tests/test_review_intake_metrics.py:42: in test_malformed_csv_rejected_before_any_writes
    with pytest.raises(ValueError): intake.import_contacts_csv(c,bid,text)
operations_nerd/insights/intake.py:30: in import_contacts_csv
    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
operations_nerd/insights/intake.py:30: in <dictcomp>
    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
E   AttributeError: 'list' object has no attribute 'strip'
_______ test_malformed_csv_rejected_before_any_writes[name,name\nA,B\n] ________
operations_nerd/tests/test_review_intake_metrics.py:42: in test_malformed_csv_rejected_before_any_writes
    with pytest.raises(ValueError): intake.import_contacts_csv(c,bid,text)
E   Failed: DID NOT RAISE ValueError
______ test_malformed_csv_rejected_before_any_writes[ NAME ,name\nA,B\n] _______
operations_nerd/tests/test_review_intake_metrics.py:42: in test_malformed_csv_rejected_before_any_writes
    with pytest.raises(ValueError): intake.import_contacts_csv(c,bid,text)
E   Failed: DID NOT RAISE ValueError
_ test_malformed_csv_rejected_before_any_writes[name,email\n"unterminated,a@x.invalid] _
operations_nerd/tests/test_review_intake_metrics.py:42: in test_malformed_csv_rejected_before_any_writes
    with pytest.raises(ValueError): intake.import_contacts_csv(c,bid,text)
E   Failed: DID NOT RAISE ValueError
_________________ test_auto_metrics_survive_final_sent_status __________________
operations_nerd/tests/test_review_intake_metrics.py:61: in test_auto_metrics_survive_final_sent_status
    assert st['auto_approved']==1 and st['human_decisions']==0
E   assert (0 == 1)
_______________ test_stats_exclude_unfinished_approved_from_sent _______________
operations_nerd/tests/test_review_intake_metrics.py:67: in test_stats_exclude_unfinished_approved_from_sent
    assert s.stats(c,bid)['by_action_type']['reply']['sent']==0
E   assert 1 == 0
=========================== short test summary info ============================
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_edit_race_preserves_only_winning_review
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_malformed_csv_rejected_before_any_writes[name,email\nA,a@x.invalid,overflow\n]
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_malformed_csv_rejected_before_any_writes[name,name\nA,B\n]
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_malformed_csv_rejected_before_any_writes[ NAME ,name\nA,B\n]
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_malformed_csv_rejected_before_any_writes[name,email\n"unterminated,a@x.invalid]
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_auto_metrics_survive_final_sent_status
FAILED operations_nerd/tests/test_review_intake_metrics.py::test_stats_exclude_unfinished_approved_from_sent
7 failed, 1 passed in 0.40s
```

## Regression after

```text
.......................................                                  [100%]
=============================== warnings summary ===============================
operations_nerd/tests/test_durable_inbox.py::test_api_enqueue_conflict_process_and_key_gate
  /tmp/ops-venv/lib/python3.10/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
39 passed, 1 warning in 2.09s
```

## Large example before

```text
2000-row CRM import: {'added': 1000, 'duplicates_skipped': 1000, 'rejected': []}
Replay: {'added': 0, 'duplicates_skipped': 2000, 'rejected': []}
500 policy decisions: {'drafted_actions': 500, 'auto_approved': 0, 'human_decisions': 0}
Mocked policy sends: 500
Completed decision edit: blocked
Traceback (most recent call last):
  File "/tmp/ops-nine-baseline/examples/review_intake_metrics.py", line 28, in <module>
    try: intake.import_contacts_csv(c,bid,'name,email\nBroken,x@example.invalid,extra\n')
  File "/tmp/ops-nine-baseline/operations_nerd/insights/intake.py", line 30, in import_contacts_csv
    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
  File "/tmp/ops-nine-baseline/operations_nerd/insights/intake.py", line 30, in <dictcomp>
    row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
AttributeError: 'list' object has no attribute 'strip'
```

## Large example after

```text
2000-row CRM import: {'added': 1000, 'duplicates_skipped': 1000, 'rejected': []}
Replay: {'added': 0, 'duplicates_skipped': 2000, 'rejected': []}
500 policy decisions: {'drafted_actions': 500, 'auto_approved': 500, 'human_decisions': 0}
Mocked policy sends: 500
Completed decision edit: blocked
Malformed import: blocked; contacts remain 1000
```
