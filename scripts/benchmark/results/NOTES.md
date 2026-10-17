### 2026-10-17 - smoke run (not a logged run): groomer T01 failed once

A 4-task smoke run (gym + groomer, T01-T04, 1 run, before the full runs) was not
logged to HISTORY because it was a filtered run, and its output was not kept.
One failure in it matters and is recorded here.

- **What happened:** groomer **T01** (create a CRM from the description). The
  model created Owner, Dog and Appointment, but wrote the entity reference as
  `ref:owner` (a prefix that is not part of the ref) in its `add_field` and
  `add_link` operations. **12 of 15 operations were refused** by the validator
  ("Entity type 'ref:owner' does not exist"), so the three entity types came
  out with no fields. T02-T04 then failed as a cascade.
- **No retry happened.** The agent turn retries once only when *every*
  operation is refused. Here the three `create_entity_type` operations were
  valid, so the proposal counted as partly valid and was shown to the owner
  as is.
- **Classification: implementation bug** (owner review). The request is
  expressible and the validator did its job, but the agent turn accepts and
  presents a proposal where 80% of the operations were refused instead of
  retrying with the refusal reasons. The harness's first-pass rule had labelled
  it "pack-authoring gap" (the model misused refs); the owner's classification
  stands because the missing retry on partial refusal is the app's behaviour,
  not the model's.
- **Not reproduced:** the next 12 groomer and bakery T01 runs (6 per full run,
  two full runs) passed with zero refused operations. A pass rate from 3 runs
  per task is a small sample; see the pass^k column.
- **Not fixed.** Agent, validator and app behaviour are unchanged on purpose.
