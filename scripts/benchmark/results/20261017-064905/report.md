# Agentic CRM benchmark report

- Run: `20261017-064905`  |  mode: **live**  |  provider: `gemini`  |  model: `gemini-3.8-flash`
- Runs per task: 3  |  businesses: gym, groomer, bakery  |  task runs: 90  |  passed: 90
- Tokens: 373,925  |  estimated cost: $0.2838 (assumed rates $0.3/M in, $2.5/M out; replace in tasks.yaml or with --price-in/--price-out)
- Wall clock: 768s  |  git HEAD: `f39d65c`
- **Code leak** (git diff on core code, everything outside scripts/benchmark/ and database files): none changed during the run; diff before: empty, after: empty.
- **Isolation**: other businesses' records byte-identical across every task: **yes**.
- Tasks run in order inside one business per run, so a failure early (especially T01) can cascade into later tasks.

## Summary (task x business)

| Task | Business | Pass | Median s | Median tokens | Median cost $ | Owner actions | Unintended record changes | Median version delta |
|---|---|---|---|---|---|---|---|---|
| T01 | gym | 3/3 | 11.7 | 5,220 | 0.00456 | 2 | 0 | 1 |
| T01 | groomer | 3/3 | 14.7 | 3,085 | 0.0057 | 2 | 0 | 1 |
| T01 | bakery | 3/3 | 14.2 | 2,878 | 0.00519 | 2 | 0 | 1 |
| T02 | gym | 3/3 | 4.8 | 4,227 | 0.0021 | 2 | 0 | 1 |
| T02 | groomer | 3/3 | 4.4 | 2,245 | 0.0015 | 2 | 0 | 1 |
| T02 | bakery | 3/3 | 4.3 | 2,170 | 0.0014 | 2 | 0 | 1 |
| T03 | gym | 3/3 | 4.6 | 4,300 | 0.00214 | 2 | 0 | 1 |
| T03 | groomer | 3/3 | 4.0 | 2,313 | 0.00138 | 2 | 0 | 1 |
| T03 | bakery | 3/3 | 4.7 | 2,210 | 0.00134 | 2 | 0 | 1 |
| T04 | gym | 3/3 | 4.9 | 4,334 | 0.00221 | 2 | 0 | 1 |
| T04 | groomer | 3/3 | 4.2 | 2,264 | 0.00141 | 2 | 0 | 1 |
| T04 | bakery | 3/3 | 4.4 | 2,209 | 0.00134 | 2 | 0 | 1 |
| T05 | gym | 3/3 | 8.0 | 4,756 | 0.00328 | 2 | 0 | 1 |
| T05 | groomer | 3/3 | 7.3 | 2,868 | 0.00254 | 2 | 0 | 1 |
| T05 | bakery | 3/3 | 9.2 | 3,050 | 0.00319 | 2 | 0 | 1 |
| T06 | gym | 3/3 | 9.0 | 5,067 | 0.00355 | 2 | 0 | 1 |
| T06 | groomer | 3/3 | 27.5 | 6,181 | 0.01036 | 2 | 0 | 1 |
| T06 | bakery | 3/3 | 10.0 | 3,329 | 0.00372 | 2 | 0 | 1 |
| T07 | gym | 3/3 | 8.2 | 9,139 | 0.00416 | 4 | 0 | 2 |
| T07 | groomer | 3/3 | 7.9 | 5,205 | 0.00275 | 4 | 0 | 2 |
| T07 | bakery | 3/3 | 8.6 | 5,012 | 0.00296 | 4 | 0 | 2 |
| T08 | gym | 3/3 | 4.3 | 4,601 | 0.00219 | 3 | 0 | 2 |
| T08 | groomer | 3/3 | 4.3 | 2,610 | 0.00147 | 3 | 0 | 2 |
| T08 | bakery | 3/3 | 4.1 | 2,521 | 0.00138 | 3 | 0 | 2 |
| T09 | gym | 3/3 | 8.4 | 9,012 | 0.00385 | 2 | 0 | 0 |
| T09 | groomer | 3/3 | 8.6 | 5,159 | 0.00278 | 2 | 0 | 0 |
| T09 | bakery | 3/3 | 8.4 | 5,124 | 0.00273 | 2 | 0 | 0 |
| T10 | gym | 3/3 | 14.2 | 5,880 | 0.00539 | 2 | 0 | 0 |
| T10 | groomer | 3/3 | 10.2 | 3,551 | 0.00375 | 2 | 0 | 0 |
| T10 | bakery | 3/3 | 21.1 | 4,789 | 0.00685 | 2 | 0 | 0 |

## Operations (summed over runs)

| Task | Business | Proposed | Passed validator | Refused by validator | Applied (approved) | Retries |
|---|---|---|---|---|---|---|
| T01 | gym | 3 | 3 | 0 | 3 | 0 |
| T01 | groomer | 49 | 49 | 0 | 49 | 0 |
| T01 | bakery | 45 | 45 | 0 | 45 | 0 |
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
| T10 | groomer | 6 | 6 | 0 | 0 | 0 |
| T10 | bakery | 3 | 3 | 0 | 0 | 0 |

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
