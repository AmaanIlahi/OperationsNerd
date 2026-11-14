# Integration plan and acceptance checkpoint

Updated November 13, 2026. Branch **logistics_integration**, based on
OperationsNerd main **4ac00e6**. The implementation combines the shared CRM with
Logistics, HR and Accounting, using the LogisticsNerd design in one application.

All nine files in the local `../meeting RAG/` folder were reviewed, including
the November 7 umbrella proposal and the later emails. The source-by-source
requirements, proposal status, evidence and limits are recorded in
[meeting_rag_acceptance.md](documentation/meeting_rag_acceptance.md). The earlier
local planning draft remains `../OperationsNerd_next_steps_plan_2026-11-12.md`.

## Implemented architecture

- One authenticated session, business picker, configurable CRM, assistant and
  schema history. Core-generated entity, field and link keys remain permanent.
- Accounts/sessions/chat usage/routing in a control DB; each business has its
  own PostgreSQL database with `core` and the schemas of its enabled modules.
- Independently keyed common Employee/Customer/Location types, extended by the
  selected business template. Modules reuse core identity through reviewed maps.
- Trusted package discovery and common role, lifecycle, command and workspace
  interfaces. No direct Nerd-to-Nerd calls; the host composes read-only reports.
- Owner-reviewed enablement at the current schema version, protected active
  dependencies, backups before module DDL, retained-history disable/re-enable.
  Failed retries cannot change the mappings of existing installation data.
- Purchasing/stock engine reused from LogisticsNerd **b16b622**, with original
  migrations unchanged and source provenance recorded. HR/accounting adaptations
  cite PR24 **702a8e6** and PR28 **3bd40cc**; the source branches were not merged.
- Native transactional operations, immutable histories and durable request replay.
  The server derives owner/actor identity; the browser cannot choose a DB or actor.
- Local PostgreSQL launcher preserves data. The existing Supabase connection
  was unavailable, and no Supabase customer data was changed.
- Baseten **openai/gpt-oss-120b**, as requested. The server keeps its key; model
  context includes current schema and enabled workflow guides, not operational
  record values. Chat messages can themselves contain owner-supplied data.
- Legacy SQLite core remains compatible, separately tested from umbrella mode.

## Requirement status

| Requirement | Result |
| --- | --- |
| Unified onboarding and UI | Implemented: no repeated Logistics master setup; shared cream/forest/lime shell and licensed local DM Sans |
| Private business DBs | Implemented locally; toy and gym use distinct DBs and schema keys; isolation verified |
| Generic enable/disable and mappings | Implemented for all three modules; invalid/stale mappings rejected, existing history cannot be retargeted after a failed retry |
| Active schema dependencies | Protected on approve/revert under a common write lock; rename allowed; incompatible types/options/links blocked |
| Master records changed after enable | Explicit synchronization and live display; immutable operational identities and approved snapshots retained |
| Submit customer order | Atomic demand creation and frozen header/lines; replay does not duplicate demand |
| Purchase/receive/reserve/ship | Implemented with split sourcing, partial receipts/shipments, whole-delivery rollback and immutable stock ledger |
| October 31 approval failure | Correct totals/budget shares and complete approval tested. Ten units at 1,000 cents total 10,000 cents. Exact historical root cause remains unproven |
| Shipment states | Event-derived draft/dispatched/in_transit/exception/delivered; delivered terminal; tracking changes no stock |
| Customer/order/shipment CSV | Templates, read-only preview, explicit atomic commit, exact amounts, reference validation, replay/deduplication; draft shipment import still needs dispatch |
| Operational reports | Current stock, outstanding POs, shipment status, overdue demand, supplier receipt timing with actual quantity denominators/missing dates |
| Explicit bills/payments | Implemented: separate draft/post/payment actions. Logistics creates no accounting documents or payments |
| Accounting reports | Trial balance, balance sheet, income statement, AR/AP aging and amount-weighted customer payment timing; USD prototype scope |
| Fund-accounting corrections | Net assets counted once; capital consumes restricted capacity; permanent release refused; temporary release reduces capacity; backdates recheck later balances |
| HR | Core employees/locations, employment history, hire/update/termination/rehire, exact half-day leave/accrual/holds, shifts and onboarding |
| Local recovery | Checksum migrations, failed-business setup retry, backup before capability install and verified isolated restore rehearsal |
| Schema-aware hosted assistant | Baseten guide/context implemented and live tested; schema changes remain proposals; no operational execution tools. Local inference remains open |
| All-module toy demo | Real browser covers one named toy business with all three modules enabled; money, HR and cross-module/business isolation checked |
| Carrier/buyer experimental drafts | PR6/PR7 ETA normalization and fork-only basket planner remain unmerged/unported; current workflow uses explicit quotes/manual tracking |
| Educational materials | All eight requested text-first scripts prepared; local browser recordings are evidence, not published translated videos |

## Verification

- **26/26 offline test scripts passed**, covering the core, legacy SQLite,
  authentication, schema/chat behavior, provider errors and WSGI guards. Real-model
  smoke is excluded from the offline runner; PG tests run separately below.
- **117 PostgreSQL tests passed**, six groups: foundation 8, purchasing/stock 52,
  host/lifecycle 16, HTTP/import/recovery/support 23, HR 10 and Accounting 8.
- A follow-up to Harsh's independent verification fixes PostgreSQL 14 tools
  overriding the local PostgreSQL 16 tools. Six tooling tests and two additional
  real backup/recovery cases cover version selection and stopping before DDL.
  The original 25-script/115-PG checkpoint remains the baseline he verified.
- HTTP tests exercise real routes and PG transactions on newly created disposable
  databases. The runner creates its own local cluster and never loads `.env`.
- An independent 16-case scope/malformed-input audit passed. Wrong owners,
  oversized/noncanonical IDs, scope conflicts, injected actors and arbitrary
  operations are rejected. Failed setup and restore tests retain the live DB.
- Browser checks cover reviewed enablement, native master records, actual-dollar
  inputs, purchase approval, receipts 4 + 6, split shipments, delivered state,
  opening stock, CSV previews/errors/reimports and retained-history disable.
- Same-business HR/Accounting browser checks cover employment/leave/shifts/tasks,
  a 5,050-cent multiline bill with 2,000/3,050 payments, invoice/payment, balanced
  trial balance, temporary grant/release/capital spending and permanent release
  refusal. Customer timing appears in Accounting and Logistics reports.
- With HR and Accounting enabled in that same business, a new ten-unit Logistics
  order completes an 80-dollar approved purchase, receipts 4 + 6 and two delivered
  shipments. Entire HR and Accounting workspace snapshots remain identical,
  proving that receipts/dispatch do not auto-create bills, payments or journals.
- Desktop and 390 px mobile checks pass without horizontal overflow or JavaScript
  errors. A business-switch response race found by the browser run was fixed.
- Live Baseten tests verify a real schema proposal remains unapplied until approval,
  saves model/token/latency audit, and explains enabled workflows without proposing
  changes. The assistant does not execute transactions.

Run the repeatable checks from the repository root:

```sh
env -u SESSION_SECRET OPERATIONS_UMBRELLA_ENABLED=0 OPERATIONS_DATABASE_URL= \
  DATABASE_PATH=/tmp/operationsnerd-tests.db LLM_PROVIDER=stub \
  .venv/bin/python scripts/run_tests.py
.venv/bin/python scripts/test_integration.py
```

Fictional browser evidence, screenshots and recordings are retained locally in
ignored `.local/verification/`. No real suppliers/carriers were contacted, labels
bought or bank transfers executed. The WSGI oversized-body test now sends headers
first to verify the pre-existing early 413 contract without a client BrokenPipe.

## Next work

The immediate project order is **integration, module videos, then local AI**.
The local manual purchase-to-shipment loop works, but the wider integration is
partial: quote recommendations, consolidated shortage review, variants/category
rules, destination validation and advanced stock controls need work. The exact
outline status is in [logistics_outline_status.md](documentation/logistics_outline_status.md).

1. Review the adapters and pinned baselines with their module owners. Agree on
   operational amendment policy, deployment ownership and release responsibilities.
2. Select actual legacy source data and define a reconciliation/migration plan.
   Finish operator cutover/rollback, off-device retention and control-DB recovery;
   [the local recovery rehearsal](documentation/integrated_recovery.md) is ready.
3. Resolve hosted deployment and separate-database provisioning permissions before
   Supabase/cloud deployment. The local proof does not establish that capability.
4. Review the paused ETA/basket drafts against the new host contract before any
   port. Preserve conservative date precedence and add combined offline cases.
5. Freeze a framework baseline and measure the work required to add another Nerd.
   Adding HR/Accounting required generic host changes in this integration; this is
   not evidence of a zero-core-change experiment on a frozen baseline.
6. Use [demo_scripts.md](documentation/demo_scripts.md) to prepare the requested
   overview and separate videos of no more than three minutes, translate the text,
   and complete the educational paper. This is sample functionality with major
   security and business-specific gaps, as the presentation source requires.

Production bank execution, refunds/credit notes, payroll, tax/FX, live carrier
qualification and automatic label generation are outside the implemented prototype.
