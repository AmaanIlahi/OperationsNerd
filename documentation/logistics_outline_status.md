# Logistics outline status

Status against the November 14 outline. Keep this order: **finish integration,
prepare the module videos, then evaluate local AI**. “Done” below means the
stated behavior works in the local prototype. It does not mean release approval.

| Outline item | Status | What works; what remains |
| --- | --- | --- |
| Select a business and review mappings | Done | One session, a private database per business and shared core records. Owners review mappings before enablement. Retargeting retained history needs a migration. |
| Order and quote inputs | Done | Whole-unit quantities, exact prices, currency checks, optional demand-line budgets, deadlines, quote capacity, expiry and expected delivery dates. Automatic budgets across several items remain open. |
| SKU, variants, units and categories | Partial | Mapped SKU/unit text and custom typed attributes work. Variant rules, unit conversion, category-dependent requirements and several category types in one stock catalog remain open. |
| Stock and destinations | Partial | Opening/on-hand/reserved/available stock works by location. Customer/location addresses are optional text. Required or frozen order destinations, address validation and route/rate lookup remain open. |
| Records and CSV | Done | Forms and customer, multiline order and draft-shipment templates work. Preview shows row errors without writes. Explicit atomic commit guards duplicates/retries and validates record IDs. Arbitrary-table imports remain open. |
| Review shortages | Partial | The server computes reserved, inbound and remaining-to-buy quantities. Owners explicitly reserve stock before buying. A consolidated planning screen is missing. |
| Compare suppliers | Partial | Quotes are listed for manual review. The tested native comparison solver is not exposed in the UI. The separate multi-item basket planner remains unported. |
| Draft, allocate, review and approve | Done | Explicit creation/allocation/approval validates price, budget, deadline and quote terms. Approval creates no stock. Native draft edit/remove functions are not exposed in this UI. |
| Partial receipt and reservation | Done | Receipts create stock and linked reservations atomically. Native delivery commands support whole-delivery rollback across lines. The form submits one line/allocation per action. |
| Ship and show remaining work | Done | Explicit reservation-based shipments, dispatch, tracking, partial fulfillment and remaining quantities work. Delivered is terminal. Tracking changes no stock. Carrier/label references are manual. |
| Outputs, history and errors | Done | Current stock, outstanding purchases, shipment states, overdue demand and supplier timing work. Enabled Accounting adds recorded payment timing. Immutable histories, replay and validation protect quantities/money. Historical stock reconstruction remains open. |
| Price failure and field guide | Partial | Tested: ten units at 1,000 cents total 10,000 cents. The October 31 failure cause remains unproven. A fresh Maple Toy Shop UI walkthrough covers the current manual flow and required inputs; wider category and destination guidance remains open. |
| Real model use | Done | Live Baseten `openai/gpt-oss-120b` produced an approval-gated schema proposal and explained receiving versus bills/payments without acting. The earlier claim that live AI was unverified is stale. |
| Schema-aware guidance | Partial | Each turn loads the current schema, module status, mappings and guides. Required active mappings are protected. Guided binding repair/replacement and complete real-model schema-change evaluations remain open. |
| Fully local AI | Open | Databases are local; Baseten inference is remote. Schema/chat context is sent; operational record values are not automatically supplied. Local inference, installation and model evaluation are absent. |
| Module videos | Partial | Eight scripts and three silent Logistics/HR/Accounting review clips exist. The reviewed, translated and published video set is unfinished. Each requested module video must be no more than three minutes. |

Use the selected business's approved `bindings` from
`GET /api/businesses/{id}/nerds` for actual entity, field and link keys. The
manifest describes their meanings. Template `crm_*` and `f_*` keys are replaced
when a business is created; they are not the live instance keys.

The current assistant explains workflows and proposes schema changes. Separate,
validated owner approval applies changes. It has no SQL or operational tools.
Orders, stock reservation, purchase approval and bill posting/payment remain
explicit owner actions. The proposed local helper must keep credentials out of
prompts and execute no schema or business changes. This stricter mode is open.

Local-AI evaluation needs three recorded cases: several items/suppliers; one
item shared across customers; and a new category with a required field. Also test
rename, removal, type changes, broken mappings and confirmed replacements.
Existing native multi-supplier/customer tests do not establish local-model success.

Evidence: native command tests under `operations_nerd/modules/logistics/tests/`,
HTTP tests under `tests_integration/`, and ignored local verification records
`browser-baseten.json`, `live-workflow-support.json`,
`all-modules-logistics-result.json` and `video-index.json` under
`.local/verification/`. See [the acceptance record](meeting_rag_acceptance.md)
and [the text scripts](demo_scripts.md) for the wider scope.

The fresh Maple Toy Shop guide and numbered screenshots are retained locally in
`.local/verification/toy-shop-walkthrough/`. The case uses two items, existing
stock, a $225 customer order, a $92 purchase, partial receipts and two shipments.
All data changes were entered through the real UI; API reads only checked state.
This is fictional local evidence, not a supplier quote or carrier qualification.
