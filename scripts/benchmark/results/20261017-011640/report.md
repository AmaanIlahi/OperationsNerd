# Agentic CRM benchmark report

- Run: `20261017-011640`  |  mode: **live**  |  provider: `gemini`  |  model: `gemini-3.8-flash`
- Runs per task: 3  |  businesses: gym, groomer, bakery  |  task runs: 90  |  passed: 90
- Tokens: 371,041  |  estimated cost: $0.3053 (assumed rates $0.3/M in, $2.5/M out; replace in tasks.yaml or with --price-in/--price-out)
- Wall clock: 851s  |  git HEAD: `9af3425`
- **Code leak** (git diff on core code, everything outside scripts/benchmark/ and database files): none changed during the run; diff before: NOT EMPTY, after: NOT EMPTY.
- **Isolation**: other businesses' records byte-identical across every task: **yes**.
- Tasks run in order inside one business per run, so a failure early (especially T01) can cascade into later tasks.

```

?? operations_nerd/tests/test_benchmark_harness.py
```

## Summary (task x business)

| Task | Business | Pass | Median s | Median tokens | Median cost $ | Owner actions | Unintended record changes | Median version delta |
|---|---|---|---|---|---|---|---|---|
| T01 | gym | 3/3 | 12.6 | 5,224 | 0.00486 | 2 | 0 | 1 |
| T01 | groomer | 3/3 | 16.4 | 3,374 | 0.00672 | 2 | 0 | 1 |
| T01 | bakery | 3/3 | 13.1 | 2,549 | 0.00467 | 2 | 0 | 1 |
| T02 | gym | 3/3 | 5.0 | 4,135 | 0.00218 | 2 | 0 | 1 |
| T02 | groomer | 3/3 | 4.5 | 2,161 | 0.00144 | 2 | 0 | 1 |
| T02 | bakery | 3/3 | 4.6 | 2,064 | 0.00136 | 2 | 0 | 1 |
| T03 | gym | 3/3 | 4.4 | 4,121 | 0.00198 | 2 | 0 | 1 |
| T03 | groomer | 3/3 | 4.3 | 2,235 | 0.00148 | 2 | 0 | 1 |
| T03 | bakery | 3/3 | 4.7 | 2,173 | 0.00144 | 2 | 0 | 1 |
| T04 | gym | 3/3 | 4.7 | 4,175 | 0.0021 | 2 | 0 | 1 |
| T04 | groomer | 3/3 | 4.2 | 2,177 | 0.00129 | 2 | 0 | 1 |
| T04 | bakery | 3/3 | 4.6 | 2,144 | 0.00142 | 2 | 0 | 1 |
| T05 | gym | 3/3 | 8.1 | 4,744 | 0.00349 | 2 | 0 | 1 |
| T05 | groomer | 3/3 | 6.3 | 2,566 | 0.00222 | 2 | 0 | 1 |
| T05 | bakery | 3/3 | 8.1 | 2,749 | 0.00288 | 2 | 0 | 1 |
| T06 | gym | 3/3 | 10.4 | 5,203 | 0.00413 | 2 | 0 | 1 |
| T06 | groomer | 3/3 | 9.4 | 3,071 | 0.00302 | 2 | 0 | 1 |
| T06 | bakery | 3/3 | 30.6 | 5,841 | 0.01011 | 2 | 0 | 1 |
| T07 | gym | 3/3 | 8.5 | 8,835 | 0.00394 | 4 | 0 | 2 |
| T07 | groomer | 3/3 | 8.1 | 4,934 | 0.00265 | 4 | 0 | 2 |
| T07 | bakery | 3/3 | 8.2 | 4,861 | 0.0028 | 4 | 0 | 2 |
| T08 | gym | 3/3 | 4.4 | 4,452 | 0.00206 | 3 | 0 | 2 |
| T08 | groomer | 3/3 | 3.6 | 2,441 | 0.00127 | 3 | 0 | 2 |
| T08 | bakery | 3/3 | 5.0 | 2,487 | 0.00151 | 3 | 0 | 2 |
| T09 | gym | 3/3 | 8.7 | 8,792 | 0.00384 | 2 | 0 | 0 |
| T09 | groomer | 3/3 | 8.9 | 4,992 | 0.00279 | 2 | 0 | 0 |
| T09 | bakery | 3/3 | 8.4 | 4,825 | 0.00268 | 2 | 0 | 0 |
| T10 | gym | 3/3 | 20.1 | 6,570 | 0.00735 | 2 | 0 | 0 |
| T10 | groomer | 3/3 | 15.5 | 4,188 | 0.00559 | 2 | 0 | 0 |
| T10 | bakery | 3/3 | 18.6 | 4,498 | 0.00657 | 2 | 0 | 0 |

## Operations (summed over runs)

| Task | Business | Proposed | Passed validator | Refused by validator | Applied (approved) | Retries |
|---|---|---|---|---|---|---|
| T01 | gym | 3 | 3 | 0 | 3 | 0 |
| T01 | groomer | 48 | 48 | 0 | 48 | 0 |
| T01 | bakery | 47 | 47 | 0 | 47 | 0 |
| T02 | gym | 3 | 3 | 0 | 3 | 0 |
| T02 | groomer | 3 | 3 | 0 | 3 | 0 |
| T02 | bakery | 3 | 3 | 0 | 3 | 0 |
| T03 | gym | 3 | 3 | 0 | 3 | 0 |
| T03 | groomer | 3 | 3 | 0 | 3 | 0 |
| T03 | bakery | 3 | 3 | 0 | 3 | 0 |
| T04 | gym | 3 | 3 | 0 | 3 | 0 |
| T04 | groomer | 3 | 3 | 0 | 3 | 0 |
| T04 | bakery | 3 | 3 | 0 | 3 | 0 |
| T05 | gym | 12 | 12 | 0 | 12 | 0 |
| T05 | groomer | 12 | 12 | 0 | 12 | 0 |
| T05 | bakery | 12 | 12 | 0 | 12 | 0 |
| T06 | gym | 3 | 3 | 0 | 3 | 0 |
| T06 | groomer | 3 | 3 | 0 | 3 | 0 |
| T06 | bakery | 3 | 3 | 0 | 3 | 0 |
| T07 | gym | 6 | 6 | 0 | 6 | 0 |
| T07 | groomer | 6 | 6 | 0 | 6 | 0 |
| T07 | bakery | 6 | 6 | 0 | 6 | 0 |
| T08 | gym | 3 | 3 | 0 | 3 | 0 |
| T08 | groomer | 3 | 3 | 0 | 3 | 0 |
| T08 | bakery | 3 | 3 | 0 | 3 | 0 |
| T09 | gym | 0 | 0 | 0 | 0 | 0 |
| T09 | groomer | 0 | 0 | 0 | 0 | 0 |
| T09 | bakery | 0 | 0 | 0 | 0 | 0 |
| T10 | gym | 3 | 3 | 0 | 0 | 0 |
| T10 | groomer | 3 | 3 | 0 | 0 | 0 |
| T10 | bakery | 6 | 6 | 0 | 0 | 0 |

## Failures

None.

## Seeded data

- gym run 1: 9 records in Member, Location, Lead
- gym run 2: 9 records in Member, Location, Lead
- gym run 3: 9 records in Member, Location, Lead
- groomer run 1: 9 records in Owner, Dog, Appointment
- groomer run 2: 9 records in Owner, Dog, Appointment
- groomer run 3: 9 records in Owner, Dog, Appointment
- bakery run 1: 9 records in Customer, Product, Order
- bakery run 2: 9 records in Customer, Product, Order
- bakery run 3: 9 records in Customer, Product, Order
