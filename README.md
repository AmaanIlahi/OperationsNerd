# OperationsNerd + LogisticsNerd

An educational business-operations prototype with one login, configurable CRM
records and explicitly enabled Logistics, HR and Accounting workspaces. Each
business has a separate PostgreSQL database. Modules reuse core master records
through owner-reviewed permanent-key mappings. This is sample functionality
with major gaps in security and business-specific functionality.

This fork starts from OperationsNerd main 4ac00e6 and carries the purchasing/stock
implementation from LogisticsNerd b16b622. See [plan.md](plan.md) for implemented
milestones, verification and remaining work.

**Run locally**

Use Python 3.12 and an isolated environment:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python scripts/run_integrated.py
```

Open http://127.0.0.1:8765/app/. Sign up using invitation **local-demo**. The
**Toy store** template contains the record types for the Logistics walkthrough.
Create records in CRM, open a workspace, review the mapped roles/fields, and approve
enablement. Enable HR and Accounting in the same way. No second onboarding or
development token is needed.

The launcher retains local PostgreSQL data and its session secret in ignored
`.local/` and never resets existing databases. Configure live chat in an ignored
`.env`; this workspace uses Baseten with **openai/gpt-oss-120b**. The provider key
stays on the server. Without a configured provider/key the launcher uses the
offline provider. See `.env.example` for other supported providers.

To use hosted PostgreSQL, set `OPERATIONS_DATABASE_URL` to a control database and,
if needed, `OPERATIONS_ADMIN_DATABASE_URL` to its provisioning connection. The
server must permit creating and connecting to separate business databases. The
existing Supabase connection was unavailable during implementation, so the
demonstrated runtime uses the local fallback. Supabase data was not altered.

**Available workflows**

- CRM, schema proposals/approval/history and one shared business selector.
- Reviewed module enablement, protected bindings and retained-history
  disable/re-enable.
- Quotes, purchase drafts, allocations and approval; atomic whole-delivery
  receiving/reservation; stock movements, shipments and tracking.
- Exact currency entry/display while storing integer minor units.
- Customer, multiline draft-order and draft-shipment CSV preview/import, with duplicate/retry
  protection and all-or-none batches.
- Read-only inventory, outstanding purchases, shipments, overdue orders, supplier
  receipt timing and recorded customer payment timing.
- HR hiring/employment history, half-day leave/accrual, shifts and onboarding.
- Accounting journals, explicit bills/invoices/post/payments, trial balance,
  statements, aging and restricted funds. Recording a payment records completed
  bank/cash movement; it does not execute a bank transfer.
- Schema-aware Baseten assistant explaining enabled workflows and proposing
  schema changes for explicit owner approval.
- Consistent private business backups, retryable setup and isolated restore
  rehearsal. See [the recovery guide](documentation/integrated_recovery.md).

Carrier, label and tracking references are entered manually. Paused ETA/basket
experiments, automatic labels, existing-data migration and production deployment
remain roadmap work. Accounting is USD-only; refunds, payroll and tax/FX are not
implemented. See [source acceptance](documentation/meeting_rag_acceptance.md)
and [text-first demo scripts](documentation/demo_scripts.md).

**Verification**

```sh
# Offline core/legacy regressions with a disposable SQLite database.
env -u SESSION_SECRET OPERATIONS_UMBRELLA_ENABLED=0 OPERATIONS_DATABASE_URL= \
  DATABASE_PATH=/tmp/operationsnerd-tests.db LLM_PROVIDER=stub \
  .venv/bin/python scripts/run_tests.py

# New disposable local PG cluster; never loads .env or uses the app database.
.venv/bin/python scripts/test_integration.py
```

Do not set SESSION_SECRET for the first command: one authentication regression
tests behavior when it is unset. The PostgreSQL runner tests the foundation,
stock engine, host adapter, actual HTTP workflows, CSV, reports, recovery, HR
and Accounting using its own
isolated connection settings. Model calls are stubbed in both suites.

The current verification checkpoint passed **26 offline scripts** and **117
PostgreSQL tests**. Real desktop/mobile browser workflows and live Baseten
checks are described in [plan.md](plan.md). Local evidence is kept in ignored
`.local/verification/`; credentials, business data and videos are not committed.

Original SQLite-mode instructions remain in
[documentation/CONTRIBUTING.md](documentation/CONTRIBUTING.md). The integrated
runtime uses the launcher above and disables unauthenticated legacy routes.
