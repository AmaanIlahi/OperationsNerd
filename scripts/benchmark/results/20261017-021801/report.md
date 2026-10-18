# Agentic CRM benchmark report

- Run: `20261017-021801`  |  mode: **live**  |  provider: `gemini`  |  model: `gemini-3.8-flash`
- Runs per task: 3  |  businesses: gym, groomer, bakery  |  task runs: 90  |  passed: 81
- Tokens: 334,966  |  estimated cost: $0.2565 (assumed rates $0.3/M in, $2.5/M out; replace in tasks.yaml or with --price-in/--price-out)
- Wall clock: 673s  |  git HEAD: `ab0069b`
- **Code leak** (git diff on core code, everything outside scripts/benchmark/ and database files): none changed during the run; diff before: empty, after: empty.
- **Isolation**: other businesses' records byte-identical across every task: **yes**.
- Tasks run in order inside one business per run, so a failure early (especially T01) can cascade into later tasks.

## Summary (task x business)

| Task | Business | Pass | Median s | Median tokens | Median cost $ | Owner actions | Unintended record changes | Median version delta |
|---|---|---|---|---|---|---|---|---|
| T01 | gym | 3/3 | 11.4 | 5,114 | 0.00459 | 2 | 0 | 1 |
| T01 | groomer | 2/3 | 13.6 | 2,948 | 0.00565 | 2 | 0 | 1 |
| T01 | bakery | 3/3 | 15.2 | 3,070 | 0.00597 | 2 | 0 | 1 |
| T02 | gym | 3/3 | 4.9 | 4,132 | 0.00219 | 2 | 0 | 1 |
| T02 | groomer | 2/3 | 4.1 | 2,091 | 0.00131 | 2 | 0 | 1 |
| T02 | bakery | 3/3 | 4.6 | 2,177 | 0.00153 | 2 | 0 | 1 |
| T03 | gym | 3/3 | 4.2 | 4,074 | 0.00189 | 2 | 0 | 1 |
| T03 | groomer | 2/3 | 3.6 | 2,210 | 0.00124 | 2 | 0 | 1 |
| T03 | bakery | 3/3 | 4.6 | 2,147 | 0.00135 | 2 | 0 | 1 |
| T04 | gym | 3/3 | 5.0 | 4,224 | 0.00223 | 2 | 0 | 1 |
| T04 | groomer | 2/3 | 4.0 | 2,153 | 0.0013 | 2 | 0 | 1 |
| T04 | bakery | 3/3 | 4.3 | 2,204 | 0.00142 | 2 | 0 | 1 |
| T05 | gym | 3/3 | 7.5 | 4,651 | 0.00327 | 2 | 0 | 1 |
| T05 | groomer | 2/3 | 6.8 | 2,901 | 0.00293 | 2 | 0 | 1 |
| T05 | bakery | 3/3 | 8.5 | 2,945 | 0.00337 | 2 | 0 | 1 |
| T06 | gym | 3/3 | 8.1 | 4,941 | 0.00349 | 2 | 0 | 1 |
| T06 | groomer | 3/3 | 7.3 | 2,837 | 0.00247 | 2 | 0 | 1 |
| T06 | bakery | 3/3 | 14.4 | 3,928 | 0.00518 | 2 | 0 | 1 |
| T07 | gym | 3/3 | 8.2 | 8,871 | 0.00407 | 4 | 0 | 2 |
| T07 | groomer | 2/3 | 8.4 | 4,916 | 0.00266 | 4 | 0 | 2 |
| T07 | bakery | 3/3 | 7.9 | 5,038 | 0.00294 | 4 | 0 | 2 |
| T08 | gym | 3/3 | 4.2 | 4,427 | 0.00201 | 3 | 0 | 2 |
| T08 | groomer | 2/3 | 3.6 | 2,458 | 0.00131 | 3 | 0 | 2 |
| T08 | bakery | 3/3 | 3.5 | 2,406 | 0.00128 | 3 | 0 | 2 |
| T09 | gym | 3/3 | 8.4 | 8,814 | 0.00394 | 2 | 0 | 0 |
| T09 | groomer | 2/3 | 8.0 | 4,894 | 0.00256 | 2 | 0 | 0 |
| T09 | bakery | 3/3 | 7.8 | 4,901 | 0.00261 | 2 | 0 | 0 |
| T10 | gym | 3/3 | 12.4 | 5,593 | 0.00492 | 2 | 0 | 0 |
| T10 | groomer | 2/3 | 8.6 | 3,209 | 0.00302 | 2 | 0 | 0 |
| T10 | bakery | 3/3 | 11.4 | 3,661 | 0.00446 | 2 | 0 | 0 |

## Operations (summed over runs)

| Task | Business | Proposed | Passed validator | Refused by validator | Applied (approved) | Retries |
|---|---|---|---|---|---|---|
| T01 | gym | 3 | 3 | 0 | 3 | 0 |
| T01 | groomer | 49 | 36 | 13 | 36 | 0 |
| T01 | bakery | 49 | 49 | 0 | 49 | 0 |
| T02 | gym | 3 | 3 | 0 | 3 | 0 |
| T02 | groomer | 2 | 2 | 0 | 2 | 0 |
| T02 | bakery | 3 | 3 | 0 | 3 | 0 |
| T03 | gym | 3 | 3 | 0 | 3 | 0 |
| T03 | groomer | 2 | 2 | 0 | 2 | 0 |
| T03 | bakery | 3 | 3 | 0 | 3 | 0 |
| T04 | gym | 3 | 3 | 0 | 3 | 0 |
| T04 | groomer | 2 | 2 | 0 | 2 | 0 |
| T04 | bakery | 3 | 3 | 0 | 3 | 0 |
| T05 | gym | 12 | 12 | 0 | 12 | 0 |
| T05 | groomer | 8 | 8 | 0 | 8 | 0 |
| T05 | bakery | 12 | 12 | 0 | 12 | 0 |
| T06 | gym | 3 | 3 | 0 | 3 | 0 |
| T06 | groomer | 3 | 3 | 0 | 3 | 0 |
| T06 | bakery | 3 | 3 | 0 | 3 | 0 |
| T07 | gym | 6 | 6 | 0 | 6 | 0 |
| T07 | groomer | 4 | 4 | 0 | 4 | 0 |
| T07 | bakery | 6 | 6 | 0 | 6 | 0 |
| T08 | gym | 3 | 3 | 0 | 3 | 0 |
| T08 | groomer | 2 | 2 | 0 | 2 | 0 |
| T08 | bakery | 3 | 3 | 0 | 3 | 0 |
| T09 | gym | 0 | 0 | 0 | 0 | 0 |
| T09 | groomer | 0 | 0 | 0 | 0 | 0 |
| T09 | bakery | 0 | 0 | 0 | 0 | 0 |
| T10 | gym | 3 | 3 | 0 | 0 | 0 |
| T10 | groomer | 5 | 5 | 0 | 0 | 0 |
| T10 | bakery | 6 | 6 | 0 | 0 | 0 |

## Failures

Classification is a first pass by rule (see `classify` in run_benchmark.py); review before acting on it.

- **T01 / groomer / run 2** (Create a CRM from a business description) - **pack-authoring gap**
    - failed `entities_have_fields`: entity types without fields: Owner, Dog, Appointment
    - why this class: The agent's operations were refused by the validator (even after the retry): Entity type 'ref:appointment' does not exist; Entity type 'ref:dog' does not exist; Entity type 'ref:owner' does not exist. The prompt or output schema did not steer it to valid operations; the request itself is expressible.
- **T02 / groomer / run 2** (Add an enum (single-choice) field to an existing entity) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'primary_entity' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T03 / groomer / run 2** (Change a field's type while it holds data) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'retypable_field' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T04 / groomer / run 2** (Rename a label (keys and data unchanged)) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'renamable_field' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T05 / groomer / run 2** (Add a new entity type linked to an existing one) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'primary_entity' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T07 / groomer / run 2** (Remove a field, then restore it) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'removable_field_a' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T08 / groomer / run 2** (Remove a field, then revert (data must come back)) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'removable_field_b' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T09 / groomer / run 2** (Bad requests (duplicate entity, unsupported field type) are refused without damage) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'primary_entity' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).
- **T10 / groomer / run 2** (Risky request - delete an entity that has records (needs approval; harness rejects)) - **pack-authoring gap**
    - failed `precondition`: no suitable target for 'primary_entity' (earlier tasks left no usable entity or field)
    - why this class: Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01).

## Seeded data

- gym run 1: 9 records in Member, Location, Lead
- gym run 2: 9 records in Member, Location, Lead
- gym run 3: 9 records in Member, Location, Lead
- groomer run 1: 9 records in Owner, Dog, Appointment
- groomer run 2: 0 records in nothing
- groomer run 3: 9 records in Owner, Dog, Appointment
- bakery run 1: 9 records in Customer, Product, Order
- bakery run 2: 9 records in Customer, Product, Order
- bakery run 3: 9 records in Customer, Product, Order
