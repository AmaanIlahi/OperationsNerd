# Meeting RAG acceptance record

Reviewed against all nine files in `../../meeting RAG/`, including
`opsnerd_umbrella.pdf`. This record separates implemented behavior, historical
claims, design proposals and deliverables that still require release/team work.
It does not turn an unanswered email question into an agreed requirement.

## Decisions applied

- Work is isolated on `logistics_integration` in OperationsLogisticsNerd,
  from OperationsNerd main `4ac00e6`. Original repositories are unchanged.
- Use locally retained PostgreSQL because the existing Supabase connection was
  unavailable; no Supabase customer data was changed. Hosted provisioning is
  still an explicit deployment decision.
- Use Baseten `openai/gpt-oss-120b` for the assistant, as requested in this chat.
  This supersedes using a local Ollama model for this integration. Schema/chat
  context goes to Baseten; the operational databases stay local.
- Supplier bills, invoices and payments require explicit actions. Purchasing,
  receiving and dispatch never automatically post or pay accounting documents.
- Carry over the LogisticsNerd visual language into the shared OperationsNerd
  shell rather than running two separate onboarding/login products.

## Source coverage

| Source file | Direction incorporated | Acceptance evidence / remaining scope |
| --- | --- | --- |
| `logisticsnerd_2026-10-31_complete_meeting.md` | Core owns master setup; permanent EAV keys; actual-dollar UX; budgets/deadlines; partial receipts and shipments; protect stock invariants | Core mappings, exact-money forms, native purchasing/stock engine, host/HTTP/browser workflow. Correct approval totals tested. The historical demo's precise failure cause remains unproven. |
| `New York University Mail - LogisticsNerd: splitting the work.pdf` | Purchasing and stock/receiving/shipping ownership; integration through explicit contracts | Reused purchasing/stock engine with provenance; host adapters only bind core identity. No wholesale teammate branch merge. |
| `My OperationsNerd branches what each one is for.pdf` | Branch purpose and ownership; avoid treating experimental work as the main baseline | Main pinned at `4ac00e6`; Logistics source pinned at `b16b622`; module provenance files record adapted source. |
| `New York University Mail - My branch notes (attached) and a few things to settle.pdf` | PostgreSQL/SQLite distinction; event-derived stock; full delivery transaction; explicit links and replay | Native PG tests cover whole-delivery rollback, concurrent last quantities, immutable movements, shared request keys and terminal delivered state. SQLite core compatibility is tested separately. |
| `New York University Mail - the inventory problem.pdf` | Product variety through configurable typed attributes; schema-aware guidance for onboarding and daily work; no autonomous actions | Custom typed fields and current-schema workflow guidance work. Baseten is the provider selected in this chat; local inference is not implemented. Category-dependent required fields, variants and a shared stock mapping across separate category types remain open. |
| `New York University Mail - OperationsNerd update: agentic CRM demo, benchmark, and next steps.pdf` | Unified agentic core; owner-approved configuration; private business databases/common records; named toy-store demo; practical reports; HR/accounting progress; accounting corrections; schema-aware workflow support | Shared session/schema history, mapped modules, distinct generated business DBs, reports, HR and explicit Accounting workflows. Four known fund failures have regression coverage. Assistant uses current schema and enabled-module guides. Benchmark/research claims are not inferred from functional passes. |
| `New York University Mail - Re: OperationsNerd.pdf` | Conservative carrier ETA normalization; combined PR6/PR7 offline evidence; fork-only multi-item buyer-plan draft; explicit merge pause | These independent drafts are not ported or merged here. The integrated workflow uses explicit supplier quotes and manual tracking. Live provider normalization and basket planning remain separate roadmap work; the email's test counts are historical claims, not results from this fork. |
| `New York University Mail - OperationsNerd paper and video.pdf` | Educational prototype with major security/business-specific gaps; overview and separate module videos of no more than three minutes; text before translation | Text scripts in `demo_scripts.md`, real browser evidence and local screen recordings where produced. Reviewed narration, translation, publication and final paper remain deliverables. |
| `opsnerd_umbrella.pdf` (November 7 architecture proposal, selectively adopted) | Control routing vs private operational data; per-Nerd schema; manifests; reviewed role mappings; dependency protection; retain disabled history; upgrade/recovery; optional future Nerds | Generic registry/lifecycle, `core`/`logistics`/`hr`/`accounting` schemas, trusted package discovery, mapped role validation and protected dependencies. Backup/restore rehearsal implemented. Cloud operations, existing-data cutover and adding further Nerds remain release/research work. |

## Functional acceptance

| Requirement | Implementation and verification |
| --- | --- |
| One login/business context | Session-authenticated middleware resolves a private business DB before handlers; control credentials stay in the control DB. Wrong-owner and malformed-ID probes fail. |
| Per-business isolation | Independent DBs and independently generated schema keys. Toy workflow leaves the gym's complete core snapshot unchanged. Module operations retain other modules' snapshots. |
| Common master catalog | Employee, Customer and Location base types; supplier/item/order types from the business template. Modules map permanent entity/field/link keys and never run a second master onboarding flow. |
| Explicit enable/review/disable | Generic proposal/approval binds role requirements at the current schema version. Failed installs retry; disable retains data; re-enable cannot silently retarget old history. |
| Active dependency protection | Core schema approve/revert validates all active module mappings under the same advisory write lock. Labels may change; mapped types, required options and links cannot silently disappear. |
| Logistics transaction boundaries | Demand freeze and host records commit atomically; deliveries aggregate and validate all portions; immutable stock ledger; serialized competing resources; durable global module request replay. |
| Purchasing policy | Exact quote totals, allocation costs, current supplier snapshots, budgets, deadlines and quote expiry remain enforced. Approval creates no stock. |
| Fulfillment | Draft/dispatch/transit/exception/delivered transitions; explicit reservation links; partial fulfillment; delivered terminal; tracking does not move stock. Manual carrier/label/tracking references are supported. |
| CSV shortcuts | Customer, multiline draft-order and draft-shipment templates, read-only preview, exact validation, explicit commit, all-or-none batches and duplicate/retry guards. Imported draft shipments still need explicit dispatch. |
| Operational reports | Current inventory, approved outstanding purchases, shipment states, overdue demand and actual supplier receipt portions. Missing comparable dates remain separate. The overdue cutoff is not a historical stock reconstruction. |
| HR | Core employee/location identity; hire/update/terminate/rehire; immutable employment history; exact half-day allowance/accrual; pending holds and leave decisions; shift conflicts; onboarding checklist history. Owner-operated prototype, not payroll or employee self-service. |
| Accounting | USD chart, balanced journals by fund, explicit bills/invoices/post/payments, unpaid document voids and journal reversals, statements and aging. Immutable posted evidence; no bank-transfer execution. Core party names are resolved without duplicating a master catalog. Refunds and credit notes are not implemented. |
| Fund corrections | Net assets equal assets minus liabilities once; designated capital spending consumes restricted capacity; permanent release refused; temporary release reduces capacity; backdating rechecks all later dates. Guards also cover direct SQL and concurrent payments. |
| Customer payment timing | Amount-weighted recorded invoice payment portions with due/payment dates, known and unknown denominators and unpaid overdue amounts, using Accounting's own current UTC cutoff. This is recorded evidence, not bank verification. Host composes Accounting's read-only view into Logistics reports only when enabled. |
| Assistant | Baseten structured output, bounded responses, safe provider errors and usage audit; schema proposals still need approval. Current schema and enabled workflow guides are context; no SQL/operational command tools or business record values are supplied. Owner chat messages may themselves contain data. |
| UI | Shared cream/forest/lime shell and licensed local DM Sans; exact dollars; generic reviewed module setup; desktop/mobile browser checks. |
| Local recovery | Repeatable launcher; checksum migration histories; business setup retry; backup before module migration; archive checksum and permissions; isolated verified restore copy. See `integrated_recovery.md`. |

## Limits of this acceptance

Functional tests establish this local educational prototype. They do not establish
production deployment readiness, accounting certification, external integration
qualification or the research hypothesis that every future Nerd requires zero
core changes. Adding HR and Accounting prompted generic framework work in module
discovery, mapping validation, lifecycle, record guards and shared UI loading;
those changes are recorded as part of this implementation, not hidden as a
successful frozen-baseline experiment.

The following remain explicit roadmap/release items: the unmerged carrier ETA
guards and standalone basket planner; category-dependent required fields and
variant rules; complete owner-facing batch/recall, damage/count correction,
return and gift-set workflows (some lot/recall functions exist only in the native
engine); a customer-selected legacy
data migration with reconciliation; live registry cutover/rollback and off-device
control/business backup policy; hosted database provisioning; live carrier/label
integration; independent review of adapted HR/accounting behavior; final paper,
translated narration and published educational videos. The initial umbrella
proposal's payroll, additional Nerds and other exploratory ideas are not claimed
implemented merely because the modules can now be enabled.
