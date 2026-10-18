# Agentic CRM benchmark report

- Run: `20261017-013108`  |  mode: **live**  |  provider: `gemini`  |  model: `gemini-3.8-flash`
- Runs per task: 3  |  businesses: gym, groomer, bakery  |  task runs: 90  |  passed: 90
- Tokens: 360,202  |  estimated cost: $0.2729 (assumed rates $0.3/M in, $2.5/M out; replace in tasks.yaml or with --price-in/--price-out)
- Wall clock: 784s  |  git HEAD: `0c1be01`
- **Code leak** (git diff on core code, everything outside scripts/benchmark/ and database files): none changed during the run; diff before: empty, after: empty.
- **Isolation**: other businesses' records byte-identical across every task: **yes**.
- Tasks run in order inside one business per run, so a failure early (especially T01) can cascade into later tasks.

## Summary (task x business)

| Task | Business | Pass | Median s | Median tokens | Median cost $ | Owner actions | Unintended record changes | Median version delta |
|---|---|---|---|---|---|---|---|---|
| T01 | gym | 3/3 | 11.1 | 5,046 | 0.00442 | 2 | 0 | 1 |
| T01 | groomer | 3/3 | 16.6 | 2,972 | 0.00571 | 2 | 0 | 1 |
| T01 | bakery | 3/3 | 11.9 | 2,458 | 0.00444 | 2 | 0 | 1 |
| T02 | gym | 3/3 | 4.6 | 4,077 | 0.00206 | 2 | 0 | 1 |
| T02 | groomer | 3/3 | 4.8 | 2,201 | 0.00143 | 2 | 0 | 1 |
| T02 | bakery | 3/3 | 4.1 | 2,245 | 0.00157 | 2 | 0 | 1 |
| T03 | gym | 3/3 | 4.2 | 4,077 | 0.00191 | 2 | 0 | 1 |
| T03 | groomer | 3/3 | 4.2 | 2,201 | 0.00134 | 2 | 0 | 1 |
| T03 | bakery | 3/3 | 4.1 | 2,189 | 0.00137 | 2 | 0 | 1 |
| T04 | gym | 3/3 | 5.3 | 4,255 | 0.00233 | 2 | 0 | 1 |
| T04 | groomer | 3/3 | 4.8 | 2,313 | 0.00151 | 2 | 0 | 1 |
| T04 | bakery | 3/3 | 4.4 | 2,208 | 0.00131 | 2 | 0 | 1 |
| T05 | gym | 3/3 | 11.3 | 4,689 | 0.00338 | 2 | 0 | 1 |
| T05 | groomer | 3/3 | 7.7 | 2,762 | 0.00261 | 2 | 0 | 1 |
| T05 | bakery | 3/3 | 8.3 | 2,776 | 0.00274 | 2 | 0 | 1 |
| T06 | gym | 3/3 | 7.9 | 4,825 | 0.00319 | 2 | 0 | 1 |
| T06 | groomer | 3/3 | 7.9 | 2,911 | 0.00247 | 2 | 0 | 1 |
| T06 | bakery | 3/3 | 14.1 | 3,825 | 0.00487 | 2 | 0 | 1 |
| T07 | gym | 3/3 | 8.9 | 8,864 | 0.0041 | 4 | 0 | 2 |
| T07 | groomer | 3/3 | 8.7 | 4,983 | 0.00269 | 4 | 0 | 2 |
| T07 | bakery | 3/3 | 7.5 | 4,997 | 0.00272 | 4 | 0 | 2 |
| T08 | gym | 3/3 | 4.4 | 4,395 | 0.00195 | 3 | 0 | 2 |
| T08 | groomer | 3/3 | 4.9 | 2,500 | 0.00129 | 3 | 0 | 2 |
| T08 | bakery | 3/3 | 4.2 | 2,534 | 0.00146 | 3 | 0 | 2 |
| T09 | gym | 3/3 | 9.3 | 8,695 | 0.0037 | 2 | 0 | 0 |
| T09 | groomer | 3/3 | 9.9 | 4,986 | 0.00273 | 2 | 0 | 0 |
| T09 | bakery | 3/3 | 8.2 | 4,911 | 0.00252 | 2 | 0 | 0 |
| T10 | gym | 3/3 | 11.6 | 5,204 | 0.00398 | 2 | 0 | 0 |
| T10 | groomer | 3/3 | 19.8 | 4,981 | 0.00757 | 2 | 0 | 0 |
| T10 | bakery | 3/3 | 10.1 | 3,332 | 0.00344 | 2 | 0 | 0 |

## Operations (summed over runs)

| Task | Business | Proposed | Passed validator | Refused by validator | Applied (approved) | Retries |
|---|---|---|---|---|---|---|
| T01 | gym | 3 | 3 | 0 | 3 | 0 |
| T01 | groomer | 49 | 49 | 0 | 49 | 0 |
| T01 | bakery | 50 | 50 | 0 | 50 | 0 |
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
