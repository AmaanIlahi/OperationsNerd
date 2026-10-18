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
- **No retry happened (at that time).** The agent turn then retried once only
  when *every* operation was refused. Here the three `create_entity_type` operations were
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
- **Fixed in `f39d65c`** ("Retry on any refused operation, add prompt example,
  reclassify partial refusals"): any refused operation now triggers one retry
  with the reasons and the exact expected shape, and the system prompt has a
  correct two-entity example. The retry logic is unit-tested (a `ref:`-style
  proposal is corrected on retry and the correct one is applied).
- **Current state: run `20261017-064905`** (commit `f39d65c`, 90/90, pass^k
  100%). The retry **did not fire** in that run: no operation was refused
  anywhere, so the fix is **not confirmed live yet**. Before the fix it
  failed once in the smoke run and once in the 18 empty-business T01 runs of the
  three earlier full runs, so a clean run of 6 such T01s is weak evidence. It is confirmed only once a live run shows a
  refusal being retried and repaired.


### 20261017-021801 - 81/90: the groomer T01 `ref:owner` failure happened again

First run after the foreign-key pragma (Harsh's PR #13 commit) and the
production route change, at commit `ab0069b`. Pass rate fell from 100% to 90%
and pass^k from 100% to 70%, entirely from **one** root failure plus its cascade:

- **groomer run 2, T01:** the same failure as the smoke run above. 13 of 16
  operations refused (`Entity type 'ref:owner' / 'ref:dog' / 'ref:appointment'
  does not exist`), 0 retries because the three `create_entity_type`
  operations were valid. The three entity types had no fields. T02-T05 and
  T07-T10 then failed as a cascade (8 tasks, "no suitable target"). T06 passed
  because an unlinked pair of entities still existed.
- **Not caused by this change.** No data changed (0 unintended record changes,
  other businesses identical) and no foreign-key or routing error occurred; the
  refusals come from the model's output, not the database. It is the second
  sighting of this failure in 20 groomer/bakery T01 runs across three full runs
  and the smoke run.
- **Classification:** the CSV row says 9 x "pack-authoring gap" because that is
  what the harness's first-pass rule outputs. Per the earlier owner review of
  this same failure it is an **implementation bug** (no retry on a mostly
  refused proposal), and the 8 cascade failures belong with it. HISTORY.csv is
  append-only, so the row is left as logged.


### Classification rule changed (commit "Retry on any refused operation, add prompt example, reclassify partial refusals")

From the commit with that subject onward, the harness classifies a failure as an
**implementation bug** (not a pack-authoring gap) when a proposal was partly
refused and not retried, or was still mostly refused (more than half of its
operations) after the retry. Later "no suitable target" failures in the same run
inherit the class of the run's first failure. T09 is exempt, since refusals there
are the expected outcome. HISTORY.csv rows logged before this change keep the
labels they were logged with (the file is append-only); use the `commit` column
to see which rule produced a row.
