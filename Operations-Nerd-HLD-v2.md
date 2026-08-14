# Operations Nerd — High-Level Design (v2)

**Status:** Updated HLD for review. Supersedes the v1 proposal ("Operations Nerd Proposal.pdf").
**Owner:** Senior researcher
**Audience:** The junior researcher (primary) + Professor Dennis Sasha
**Companion diagrams:** `HLD-v2-architecture.drawio`, `HLD-v2-runtime-pipeline.drawio`, `HLD-v2-sequence-realestate.drawio`, `HLD-v2-data-model.drawio`

---

## 0. What changed in v2 — a senior's read

v1 is a strong architectural thesis. The **config-first, shared-ontology, vertical-adapter** pattern is the right shape. Where v1 is thin, the junior is hand-waving things that will collapse in production. v2 keeps the same thesis and adds the engineering substrate it needs to actually work:

| Gap in v1 | v2 fix |
|---|---|
| 9-stage pipeline conflates one-time setup with per-event processing | **Two pipelines.** `Onboarding Pipeline` (one-time per business) vs `Event Runtime Pipeline` (per inbound event). Stage 7 in v1 was actually onboarding, not runtime. |
| Hidden approval gate is mentioned in §4 but not modeled as a stage | **Explicit HITL Gate** between Action Drafting and Action Execution, with confidence threshold + action classification. |
| Ontology is 4 primitives (Contact, Transaction, Task, Follow-up) — too thin to model real CRM/ERP | **Two-layer ontology** à la O-CREAM-v2: upper core (universal, ~6 entities) + per-vertical lower core. Contact and Transaction are *aggregates*, not flat records. |
| No data model, no schema, no multi-tenant isolation | **Postgres with RLS** using `tenant_id` on every tenant-scoped table and `app.current_tenant` GUC. Shared schema, RLS-enforced isolation. |
| No durability story — what if a worker dies mid-pipeline? | **Durable execution** via Temporal (outer workflow) + LangGraph (inner reasoning), with checkpoint-per-stage and event-sourced replay. |
| No outbox / no saga for side effects | **Transactional outbox + saga compensations** for every side-effecting action. Four-tuple idempotency key `(agent_run_id, step_id, tool_name, business_scope)`. |
| Compliance check (Stage 5) is hand-wavy | **Pluggable policy engine** (OPA/Rego + JSON rules + LLM-as-judge) with audit log. Separate from the runtime LLM, so it's auditable. |
| Saved-state "versioned, reloadable" is one sentence | **Snapshot model** à la Replit: adapter versioned like git, DB snapshots for in-flight event rollback, immutable history. |
| No process-discovery loop — how do the rules get into the saved-state in the first place? | **Discovery loop** that mines the business's historical events (analogous to PMAx / pm4aa) and proposes adapter edits. |
| No evaluation plan | **Four-layer eval**: capability benchmarks (CRMArena-style), online monitoring (override rate, completion rate, drift), human feedback loop, business KPIs. |
| No failure-mode catalog | **Seven failure classes** enumerated, each with a specific guard. Mirrors the practitioner literature (Fiddler 2026, gravity.fast 2026). |
| No deployment story | **Runtime topology**: web console, webhook receiver, worker pool, event bus, durable store, snapshot store. |
| CustomNerd relationship is acknowledged but not contrasted | **What's actually new**: side-effecting actions, real money in the loop, compliance-bounded, multi-tenant, durable, audited. |

The rest of this document is the v2 design.

---

## 1. Problem statement (validated, tightened)

### 1.1 Empirical case

The v1 numbers (median 5 AI tools per SMB, $450B vertical SaaS, 40% of enterprise apps shipping with agents) are right. The 2026 picture sharpens it:

- **Gartner (May 2026):** AI agent software spend $86.4B (2025) → **$206.5B (2026) → $376.3B (2027)**. 139% YoY. Fastest-growing enterprise software category.
- **a16z "AI Eats Vertical SaaS":** 30–40% of the $450B vertical SaaS market projected to be reshaped by AI agents in 2026–2028.
- **Upwork Research Institute (Q1 2026, n=750):** 62% of SMB leaders "very confident" handing high-stakes tasks to AI agents; 1-in-3 call agents mission-critical.
- **SBE Council (Apr 2026):** 82% of small-business employers have invested in AI tools, median **5 tools in active use**; 62% plan to spend more.
- **Gartner:** ROI uncertainty is the #2 adoption barrier (24%), behind data security/compliance (27%).

The consolidation thesis in v1 — *one system should replace several point tools* — is now the dominant strategy of vendors (Square Managerbot, ServiceTitan Atlas, Freshworks vertical agents, ADP / Paychemp / Intuit / Samsara) and the explicit ask of buyers.

### 1.2 What the consolidation gap actually is

| Category | Closest examples | Why it falls short (still true in 2026) |
|---|---|---|
| Horizontal no-code builders | Zapier Central, Relevance AI, Lindy, Airtable Omni, n8n | Manual setup per business, no reusable saved-state snapshot |
| Vertical point solutions | Salesforce, HubSpot, monday CRM, ServiceAgent.ai | Accounting is a bolt-on integration, never native |
| AI OS platforms | PwC agent OS, Ema, Decidr.ai | Enterprise-scale, consulting-led, not self-serve for a solo clinic |

The gap is **not "no AI tools"** — the gap is **"no config-first, multi-vertical, self-serve system that consolidates the four universal SMB operations functions (relationship, sales, outreach, accounting) into one tenant-isolated durable runtime"**. Operations Nerd targets that exact gap.

### 1.3 What the literature says is hard

Two results shape the design more than anything else:

- **CRMArena-Pro (Salesforce AI Research, May 2025):** best LLM agents achieve only **58% single-turn** success and **35% multi-turn** on realistic CRM tasks across 19 task types. Even Gemini 2.5 Pro, the best model, hits 83% on workflow execution but collapses on policy compliance and multi-turn. Agents also show **near-zero inherent confidentiality awareness**.
- **Fiddler / gravity.fast 2026 practitioner studies:** real-world agent failure rate **70–95%** on production tasks. **88% of agents that pass controlled demos fail in production.** Failure modes are predictable: hallucination cascade, scope drift, silent confidence failure, runaway loops, wrong-tool selection, prompt injection, state drift.

Implication: a config-first system that asks an LLM to make decisions about money, customer data, and compliance checks must be built around the assumption that the LLM **will** fail. The design must make failure recoverable, observable, and bounded — not prevent it. This is the central pivot in v2 vs. v1.

### 1.4 Relationship to CustomNerd (sharpened)

CustomNerd is read-mostly, text-only, single-tenant-research, and was evaluated as a research contribution against RAG/agentic baselines. Operations Nerd is read-and-write, produces real-world side effects, multi-tenant, and is scoped as an application deliverable. The architectural thesis is the same (one config-driven pipeline, many domains, behavior externalized); the engineering substrate is fundamentally different (durable execution, outbox/saga, HITL gates, multi-tenant RLS, audit log, snapshot model). The custom-evaluator approach in CustomNerd — measure retrieval accuracy — is the wrong metric here; v2 instead uses task completion rate and human override rate as the primary business metrics (see §9).

---

## 2. Architecture overview

Operations Nerd is a **multi-tenant SaaS** with three planes:

```
┌─────────────────────── PLANE 1: ONBOARDING (one-time) ───────────────────────┐
│ Setup Questionnaire → Adapter Composer → Saved-State Snapshot (versioned)  │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       │ reloads
                                       ▼
┌─────────────────────── PLANE 2: EVENT RUNTIME (per inbound event) ───────────┐
│ Ingest → Validity Gate → Ontology Conversion → Context Extraction →         │
│ Function Classification → Compliance & Safety → Action Drafting →          │
│ [HUMAN APPROVAL GATE if needed] → Action Execution (via outbox/saga) →     │
│ Audit Log + Live Feedback                                                  │
└──────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       │ proposes edits
                                       ▼
┌─────────────────────── PLANE 3: DISCOVERY & EVAL (continuous) ──────────────┐
│ Event-log mining → Process discovery → Adapter diff proposals → Owner review│
│ Outcome tracking → Drift monitoring → Compliance audit                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Three planes, three cadences, three ownership models. This is the load-bearing change from v1. See `HLD-v2-architecture.drawio` for the full container view.

### 2.1 Hexagonal core

The core follows **Ports & Adapters (Cockburn)**: the business logic — the runtime pipeline + saved-state contract — is the hexagon. Everything vertical-specific (the ontology mappings, the integration connectors, the compliance rules, the action templates) is an adapter. The discovery and onboarding planes are also adapters, not core.

Why this matters: the core is **domain-agnostic**. Vertical-specific code lives in a swappable adapter. The thesis is the same as v1, but the hexagon/adapter boundary is now explicit, so we can test the core against multiple verticals without code changes.

### 2.2 Runtime topology (containers)

| Container | Responsibility | Tech (indicative) |
|---|---|---|
| Web Console | Owner UI: events, approvals, snapshots, analytics | React + SSE |
| API Gateway | Auth, tenant context, rate limit, request routing | API layer (FastAPI / equivalent) |
| Event Ingest | Webhooks (Gmail, Outlook, Twilio, Stripe, QuickBooks, generic form) + polling fallback + CSV import | Receivers per source |
| Runtime Worker | Executes the event runtime pipeline; durable via Temporal | Temporal worker + LangGraph inner |
| Adapter Composer | Onboarding questionnaire → saved-state | Web + composer service |
| Snapshot Store | Versioned saved-state, DB snapshots, immutable history | Postgres + object store (S3-like) |
| Compliance Engine | Pluggable policy evaluation (OPA/Rego + JSON rules + LLM-as-judge) | OPA sidecar + policy bundle |
| Discovery Service | Process mining from event log, adapter-diff proposals | Process-mining library + LLM analyst |
| Policy / Secrets Store | Tenant secrets, integration tokens, encryption keys | Vault / KMS-equivalent |
| Event Bus | Internal pub/sub for stage events, audit, live feedback | Durable queue (Kafka/equivalent) |
| Observability | Traces, metrics, logs, override rate, completion rate | OpenTelemetry → backend |

All tenant data lives in a single Postgres logical cluster with **RLS-enforced tenant isolation**. Snapshots live in object storage with per-tenant prefixes.

### 2.3 What runs where

- **Synchronous UI requests** (read state, approve action) → API Gateway → Postgres.
- **Asynchronous event processing** → Event Ingest → Event Bus → Runtime Worker (durable via Temporal) → Postgres (state + outbox) → Event Bus (downstream).
- **Side-effecting actions** (send email, post invoice, book calendar) → Outbox drainer → adapter-specific connector. Never from inside the LLM call.
- **Live feedback** to UI → SSE stream from Runtime Worker, per `event_id`.
- **Discovery** → batch job over event log, async, with owner review before any adapter edit is applied.

---

## 3. Two-layer ontology

v1 has four primitives: Contact, Transaction, Task, Follow-up. That is too thin to model real SMB operations. Two problems:

1. The primitives are flat — no relationships, no hierarchies, no audit, no events.
2. They are forced to do too much work — Follow-up is a Task + a log + a status. A real ontology separates these.

v2 uses a two-layer ontology modeled after **O-CREAM-v2 (Cairns et al., 2010)** and **CURIE-O (Sales et al., 2021)**: an upper core (universal across verticals) and per-vertical lower cores.

### 3.1 Upper core (universal — these exist in every vertical)

| Entity | Purpose | Example instantiations |
|---|---|---|
| **Party** | The actor on the world. | Patient (clinic), Client (real estate), Lead (sales), Vendor (accounting) |
| **Party Relationship** | Edges between Parties. | Referred-by, household, employer, co-borrower |
| **Engagement** | A point of contact or a unit of work that produces evidence. | Call, email, visit, form submission, document received |
| **Agreement** | A commitment that frames a set of Engagements and Transactions. | Listing agreement, treatment plan, retainer |
| **Transaction** | An exchange of value (money, goods, services). Has line items. | Invoice + line items, payment, refund, charge |
| **Obligation** | A pending Task bound to a Party / Agreement / Transaction. | Recall reminder, follow-up call, contract renewal |
| **Event** | Anything that happens in the system. The log spine. | Inbound email, status change, action taken, system tick |
| **Snapshot** | A versioned bundle of adapter state. | Adapter v1, adapter v2 |

Eight upper-core entities (vs. four in v1). Each is small, has a stable identity, and is independent of vertical.

### 3.2 Lower core (per-vertical — see §3.3)

Each vertical defines its own lower-core subclasses and refines upper-core entities with domain attributes. The lower core is part of the saved-state snapshot, not the core engine.

Example — **real-estate lower core**:
- `Party` refines into `Lead`, `Buyer`, `Seller`, `Co-buyer`, `Vendor`, `TitleCompany`.
- `Engagement` refines into `PropertyShowing`, `OpenHouseVisit`, `ContractReview`, `ClosingCall`.
- `Transaction` refines into `Commission`, `EarnestMoneyDeposit`, `TitleFee`, `ClosingCost`.
- `Obligation` refines into `DisclosureDeadline`, `ContingencyRemoval`, `ClosingChecklist`.

Example — **clinic lower core** (HIPAA-bounded):
- `Party` refines into `Patient`, `ReferringProvider`, `InsuranceCarrier`, `Guarantor`.
- `Engagement` refines into `Appointment`, `TelehealthVisit`, `LabResultReceived`, `Recall`.
- `Transaction` refines into `Claim`, `PatientResponsibility`, `InsurancePayment`, `Adjustment`.
- `Obligation` refines into `RecallReminder`, `PreAuth`, `LabOrderFollowup`, `RxRefill`.

The lower core is what makes the system "feel like" a real-estate system or a clinic system. The upper core is what makes it one system.

### 3.3 Why this beats the v1 ontology

- **O-CREAM-v2 explicitly targets SMEs** — same population as Operations Nerd — and finds that a two-layer ontology is the minimum needed to model CRM/operations without becoming vertical-specific itself.
- **Relationships, audit, and events are first-class** — the v1 ontology has no place for "this Patient was referred by that Patient" or "show me the log of every change to this Transaction." v2 puts those in the upper core.
- **Adapter state is data, not code** — the lower core is JSON-Schema-validated config in the saved-state snapshot. v1's "adapter" was implicit.

---

## 4. Event runtime pipeline (per inbound event)

This is the clean per-event pipeline. v1's Stage 7 (Adapter Setup Questionnaire) is **not** in here — it lives in the Onboarding plane (see §5).

| # | Stage | What it does | Failure guard |
|---|---|---|---|
| 1 | **Ingest** | Webhook / polling / CSV row arrives at Event Ingest. Tenant resolved from credentials. Idempotency key from source (`message_id`, `event_id`, `row_id`) | Dedupe by `(tenant_id, source, source_event_id)` |
| 2 | **Validity Gate** | Confirms the input is a legitimate operational event (not spam, not malformed, not a system echo) | LLM-as-judge with a strict prompt + rule-based prefilter |
| 3 | **Ontology Conversion** | Converts raw input to one or more upper-core entities (Party / Engagement / Transaction / Obligation / Event) using the loaded lower-core ontology | JSON-Schema validation; reject if no entity can be produced |
| 4 | **Context Extraction** | Pulls supporting data: existing Party record, prior Engagements, related Transactions, calendar entries, payment status | Read-only by default; runs as Temporal activity |
| 5 | **Function Classification** | Routes the event to one of the four functions (relationship / sales / outreach / accounting) and to the matching vertical-adapter logic | Classification is a constrained LLM call with classifier + rule fallback |
| 6 | **Compliance & Safety** | Pluggable policy engine: HIPAA / licensing disclosure / financial thresholds / PII redaction. Plus a soft LLM-as-judge for novel inputs | All policy decisions logged to Audit Log with input hash + decision + reason |
| 7 | **Action Drafting** | Generates the *draft* action: a typed action object (`SendEmail`, `PostInvoice`, `CreateCalendarEntry`, `LogTransaction`, `SetObligation`) with full parameters and the evidence used | Draft is a pure object — never executed. Action classified as `auto`, `requires_approval`, or `block` |
| 8 | **HITL Gate** | If action is `requires_approval`, posts to Approval Queue. If `auto`, proceed. If `block`, do not execute; surface to owner for review | Default = `requires_approval` for `send_external`, `create_financial`, `modify_record`. Owner-configurable per vertical. |
| 9 | **Action Execution (outbox + saga)** | Outbox row written in same transaction as the state change. Drainer dispatches to the adapter. Saga records forward + compensating action. Idempotency key = `(agent_run_id, step_id, tool_name, business_scope)`. | Compensation on failure. Dead-letter to Owner Console on permanent failure. |
| 10 | **Audit + Live Feedback** | Stage 10a: every stage appends a structured `Event` row (XES-extended) to the per-tenant event log. Stage 10b: SSE push to Web Console for live progress. | All events are append-only and tenant-scoped. |

### 4.1 What v1 had that v2 removes from this list

- **Stage 7 (Adapter Setup Questionnaire):** moved to Onboarding (see §5).
- **Stage 8 (Saved-State Export):** moved out of per-event. The snapshot is the *result* of onboarding, loaded at the start of every event run. Snapshotting is a separate concern.
- **Stage 9 (Live Pipeline Feedback):** kept, but as the *last* stage of every event, not a side-process.

### 4.2 What v2 adds

- **Stage 1 Ingest** with idempotency (missing in v1; critical for retries).
- **Stage 8 HITL Gate** (v1 buried this in §4 prose; now first-class).
- **Stage 9 Outbox + saga** (v1 had zero reliability for side effects; 88% of agents that pass demos fail in production without this).
- **Stage 10 split into 10a (audit log) + 10b (live feedback)**.

### 4.3 Where CustomNerd shows up

Stages 2–7 are conceptually similar to CustomNerd's expertise-driven Q&A pipeline: validity → extraction → routing → safety → answer. The new surface in Operations Nerd is **stages 8–10**: real-world side effects need durability, idempotency, compensation, approval, and audit. That is what the engineering effort is for.

---

## 5. Onboarding pipeline (one-time per business)

This is what v1 called "Stage 7." It runs once per business, produces the saved-state snapshot, and is **not** a per-event stage.

```
[New Business Signup]
   ↓
[Step 1: Pick vertical]   ← clinic | real-estate | sales | custom
   ↓
[Step 2: Connect integrations]   ← Gmail/Outlook, Stripe, calendar, etc.
   ↓
[Step 3: Seed from history]   ← import last 90 days of relevant data
   ↓
[Step 4: Discovery loop]   ← mine event log → propose workflow patterns
   ↓
[Step 5: Adapter Questionnaire]   ← confirm/edit proposed rules
   ↓
[Step 6: Compliance profile]   ← pick policy bundle (HIPAA, real-estate, etc.)
   ↓
[Step 7: Compose snapshot v1]   ← adapter + ontology + policies + integrations
   ↓
[Step 8: Test-mode 24h]   ← run in shadow mode, no real actions
   ↓
[Step 9: Promote v1]   ← loaded by all subsequent event runs
```

The **Discovery loop (Step 4)** is the bridge between Onboarding and Discovery/eval (Plane 3). It is what makes the saved-state **data-driven** rather than hand-authored. This addresses v1's biggest gap: how the rules get into the saved-state in the first place.

---

## 6. Saved-state snapshot (the actual deliverable per business)

A snapshot is a **versioned, immutable, replayable bundle** of the per-business adapter state. The model follows Replit's snapshot engine:

```
snapshot = {
  schema_version:    "opsnerd.snapshot.v2",
  snapshot_id:       "snap_<uuid>",
  parent_snapshot_id: "snap_<uuid>"  | null,  // for forking/rollback
  tenant_id:         "<uuid>",
  created_at:        <timestamp>,
  created_by:        "<user_id>",
  provenance:        "onboarding" | "discovery_proposal" | "manual_edit" | "rollback",

  ontology:          { ... lower core ... },         // §3
  integrations:      { ... tokens, scopes ... },     // §2.2
  policies:          { ... rule bundle, OPA Rego ... }, // §7
  action_templates:  { ... per-vertical action defs ... },
  approval_policy:   { ... per-action-type ... },    // §4 stage 8
  discovery_rules:   { ... mining config ... },      // §8

  hash:              "<sha256 of canonical JSON>"     // integrity
}
```

### 6.1 Versioning, diff, rollback

- Every snapshot is **immutable**. Edits produce a new snapshot with `parent_snapshot_id` set.
- The `hash` is computed over canonical JSON, so any change is detectable.
- A snapshot is the *adapter state* at a point in time. It is paired with a **DB snapshot** for in-flight events so they can be safely resumed.
- **Rollback** = load the parent snapshot and resume in-flight events from their checkpoint. The history is append-only.
- The owner can diff any two snapshots in the Web Console.

This is what v1's "versioned, reloadable" should have been.

### 6.2 What's in the saved-state vs what's in the core

| In the core (engine code) | In the saved-state (per tenant) |
|---|---|
| Pipeline stage logic | Ontology lower-core |
| Outbox / saga dispatcher | Policy bundle |
| HITL gate machinery | Action templates |
| Event log writer | Approval policy per action type |
| Durable execution substrate | Integration credentials |
| Multi-tenant routing | Compliance profile selection |

The thesis in v1 — *behavior externalized into configuration rather than code* — is preserved and made concrete. The rule: anything that differs per business goes in the snapshot; anything that is the same across all businesses stays in the core.

---

## 7. Compliance & safety (pluggable policy engine)

v1's Stage 5 is a one-sentence box. v2 makes it a real subsystem with three layers:

### 7.1 Layer 1 — Rule-based (deterministic)

- HIPAA Safe Harbor identifiers → must be redacted before any LLM call sees them.
- Financial thresholds (e.g., "no invoice > $X without approval").
- PII detection regex + entropy heuristics.
- Tenant-specific allow/deny lists (e.g., "do not contact this list").
- **Engine:** OPA / Rego policies loaded from the snapshot's `policies` field.

### 7.2 Layer 2 — LLM-as-judge (soft, narrow scope)

- For novel inputs, a constrained judge model with a strict system prompt evaluates compliance.
- Judge is **separate** from the runtime LLM (different model, different prompt, separate log).
- Judge always returns: `pass` / `flag` / `block` + reason + evidence excerpt.
- Flag and block decisions route to HITL.

### 7.3 Layer 3 — HITL escalation

- Anything the rule engine and judge disagree on, or anything tagged `block`, goes to the owner.
- Owners can promote a `block` to a `policy` rule (so it never reaches the LLM again).

All three layers write to the **Audit Log** with: input hash, policy ID, decision, reason, evidence, latency, model version. This is what makes the system auditable in a regulated setting (HIPAA, financial services).

---

## 8. Human-in-the-loop (HITL) — explicit design

v1 mentions "drafts then approval" in §4 but never designs it. v2 makes HITL a first-class subsystem.

### 8.1 Action classification

Every drafted action is classified along two axes:

- **Reversibility:** `reversible` (e.g., create internal task) vs `irreversible` (e.g., send external email, post financial transaction).
- **Risk surface:** `internal` vs `customer-facing` vs `financial` vs `regulated`.

The combination determines the default approval policy:

| Reversibility | Risk | Default |
|---|---|---|
| reversible | internal | `auto` |
| reversible | customer-facing | `auto` (with audit) |
| irreversible | internal | `requires_approval` |
| irreversible | customer-facing | `requires_approval` |
| irreversible | financial | `requires_approval` |
| irreversible | regulated (HIPAA, etc.) | `block` (escalate to owner) |

Owners can override the default per action type in the saved-state. Per the production practice literature (Galileo, dev.to HITL patterns), this should be **per action type**, not per event, and the override itself is auditable.

### 8.2 Approval Queue (the UI)

- Inbox-style list: pending actions, sorted by age and risk.
- Each item: drafted action, evidence, policy decisions, suggested response, confidence score.
- Owner can **approve**, **edit**, **reject**, or **promote-to-policy** (turn this specific decision into a standing rule).
- Every action in the queue is linked back to the event that produced it (full replay).

### 8.3 Confidence threshold

The runtime LLM returns a calibrated confidence score with each drafted action. The threshold for `auto` vs `requires_approval` is set per action type, with a default of 0.85. The literature is clear: do not use a single LLM confidence score alone; combine with rule-based signals and historical accuracy. v2 uses **three signals** (LLM confidence, rule coverage, historical accuracy on this action type) and routes to approval if any one is below threshold.

### 8.4 What the literature says

- HITL reduces critical error rate by 78% in production (23% → 5.1%) per dev.to 2026 practitioner data.
- Default for irreversible = approval (Fiddler, gravity.fast 2026).
- The HITL override rate is the most reliable leading indicator of system quality; rising override rate predicts a quality incident within a week.

These become concrete metrics in §9.

---

## 9. Evaluation plan

v1 is silent on evaluation. v2 has a four-layer plan based on the production-agent literature (Galileo, thinking.inc 2026; arXiv 2507.21504):

### 9.1 Layer 1 — Capability benchmarks (offline)

A **CRMArena-style benchmark for SMB operations**, in the spirit of CRMArena-Pro. The benchmark has:

- 8–10 task types per vertical (real estate, clinic, sales).
- Tasks generated from synthetic data with realistic inter-entity dependencies (a real estate offer has 12+ linked records, like the CRMArena 16-object setting).
- A mix of single-turn, multi-turn, and confidentiality checks.
- LLM-agnostic scoring: exact match for transactional tasks, rubric for soft tasks.

Reported metrics: single-turn success rate, multi-turn success rate, policy compliance rate, confidentiality awareness rate. **Target:** ≥ 90% on workflow execution (matches CRMArena-Pro ceiling for top models), ≥ 80% on policy compliance, ≥ 95% confidentiality.

### 9.2 Layer 2 — Online monitoring (production)

- **Task completion rate:** % of events that produce a usable output without human correction. Target: ≥ 90% on routine events.
- **Human override rate:** % of approval-queue items where the owner rejects or edits. Alert if it rises > 5% week-over-week.
- **Stage latency:** p50 / p95 / p99 per stage.
- **Outbox lag:** pending outbox rows older than N seconds.
- **Saga compensation rate:** % of executed actions that required compensation.
- **Override drift:** rising override rate predicts a quality incident; alert on trend.

### 9.3 Layer 3 — Human feedback loop

- Every owner approval / edit / reject / promote-to-policy is captured as labeled training signal.
- Weekly batch: re-evaluate the agent on the labels and produce a small adapter diff (e.g., "add rule: never auto-send to addresses matching `/noreply/`").
- Adapter diffs go to owner review before promotion.

### 9.4 Layer 4 — Business KPIs

The metric that justifies the product, not the agent:

- **Time saved per business per week** (target: ≥ 5 hours).
- **% of outbound messages that receive a response** (proxy for quality).
- **Days-sales-outstanding for invoices** (accounting function).
- **Customer-reported satisfaction** (relationship function).

These are the only metrics that matter for the application-deliverable framing in §1.4.

---

## 10. Failure modes (and the v2 guard for each)

| Failure mode | What it looks like | v2 guard |
|---|---|---|
| **Hallucination cascade** | LLM emits plausible-but-wrong entity; downstream actions compound | Validity Gate rejects malformed conversions; HITL for irreversible actions; audit log catches downstream use |
| **Broken process amplification** | Agent faithfully reproduces a flawed workflow at machine speed | Discovery loop surfaces deviations from logged reality; HITL allows per-action override; promote-to-policy closes the loop |
| **Scope drift** | Agent decides to call APIs or touch data outside the saved-state | Compliance engine + scope policy in the snapshot; stage 6 rejects out-of-scope actions; audit log alerts |
| **Silent confidence failure** | Agent produces confident wrong output, no error signal | Three-signal confidence threshold (LLM + rules + history); HITL default for irreversible; override rate monitoring |
| **Wrong-tool selection** | Agent picks the wrong connector for an action | Action templates in the snapshot constrain the action shape; tool selection is deterministic given the action type; HITL catches |
| **Runaway loops / cost** | Agent re-enters a stage, consumes unbounded tokens | Stage-level timeouts; per-event token budget; saga timeouts; alert on tail-latency drift |
| **Prompt injection** | Inbound content contains instructions targeting the LLM | Input sandboxing + structural prompt separation + LLM-as-judge for suspicious content; outbound actions require HITL |
| **State drift** | System state and agent's view of state diverge | Durable execution with checkpoint per stage; event-sourced audit log; saga compensations; outbox guarantees |
| **Evidence gap** | Owner cannot tell why an action was taken | Every action carries a typed `evidence` object: input hash, retrieval hits, rule decisions, LLM trace, model version |

These are the seven classes from gravity.fast 2026 plus the two most-cited practitioner patterns. v2 maps a specific guard to each.

---

## 11. Discovery loop (Plane 3)

The Onboarding Pipeline (Plane 1) runs once. The Discovery Loop (Plane 3) runs continuously. Its job is to **mine the business's actual event log** and propose adapter edits.

### 11.1 What it does

- Reads the per-tenant event log (XES-extended: stages, tool calls, LLM traces, token costs).
- Runs process discovery (à la PMAx / pm4aa) to surface:
  - **Recurring patterns** the saved-state doesn't cover (e.g., "we send a confirmation SMS after every invoice — but the snapshot doesn't have an SMS adapter").
  - **Common deviations** (e.g., "30% of leads are touched by owner before stage 5 — the rule says they should be auto-routed").
  - **Unused rules** (e.g., "rule `R-12` has never fired in 60 days — candidate for removal").
  - **Compensation hot spots** (e.g., "compensation rate for `PostInvoice` is 8% — investigate").

### 11.2 What it produces

- A **discovery report** (per business, weekly).
- A **candidate adapter diff** (proposed edits to the saved-state, with provenance and expected impact).
- Diffs go to owner review in the Web Console. Approved diffs create a new snapshot version (parent = current).

### 11.3 Why this is the right pattern

Process mining on event logs is a 20-year discipline (van der Aalst). Combining it with LLM interpretation (PMAx, pm4aa) is the established agentic pattern. Operations Nerd has the unusual advantage that it **owns the event log** — every stage writes to it — so the discovery loop has rich, structured, complete data to mine. v1 left this as an open question; v2 makes it a subsystem.

---

## 12. Data model (informal)

Per-tenant tables, all with `tenant_id UUID NOT NULL` and RLS enabled. The schema is shaped by §3 (two-layer ontology).

```
upper_core:
  parties             (id, tenant_id, kind, lower_core_subtype, attrs jsonb, created_at, updated_at)
  party_relationships (id, tenant_id, from_party, to_party, kind, valid_from, valid_to)
  engagements         (id, tenant_id, party_id, kind, occurred_at, evidence jsonb)
  agreements          (id, tenant_id, primary_party_id, kind, effective_from, effective_to, attrs jsonb)
  transactions        (id, tenant_id, agreement_id, kind, amount_cents, currency, occurred_at, attrs jsonb)
  transaction_lines   (id, tenant_id, transaction_id, sku, qty, unit_amount_cents, attrs jsonb)
  obligations         (id, tenant_id, party_id, kind, due_at, status, attrs jsonb)
  events              (id, tenant_id, kind, occurred_at, stage, input_hash, decision, evidence jsonb, prev_event_id)  -- append-only
  snapshots           (id, tenant_id, parent_snapshot_id, hash, created_at, created_by, provenance, payload jsonb)

runtime:
  outbox              (id, tenant_id, four_tuple_key, payload jsonb, status, attempts, last_error, next_attempt_at)
  sagas               (id, tenant_id, kind, state, forward jsonb, compensation jsonb, started_at, completed_at)
  approvals           (id, tenant_id, event_id, action jsonb, decision, decided_by, decided_at)
  audit_log           (id, tenant_id, ts, actor, action, target, input_hash, decision, reason, evidence)

per_tenant_secrets (encrypted, never in snapshots):
  integration_tokens  (id, tenant_id, integration, encrypted_token, scopes, last_rotated_at)
```

Plus **per-vertical lower-core tables** as needed (e.g., `real_estate_listings`, `clinic_appointments`), each with `tenant_id` and RLS.

### 12.1 RLS pattern

```sql
-- Per-connection GUC set at the start of every request.
SET app.current_tenant = '<uuid>';

-- Policy on every tenant-scoped table.
CREATE POLICY tenant_isolation ON parties
  FOR ALL TO app_user
  USING (tenant_id = current_setting('app.current_tenant')::uuid);
```

The application role `app_user` is non-superuser; RLS is forced (`FORCE ROW LEVEL SECURITY`). The DB owner role is used only for migrations.

### 12.2 Why this beats v1

v1 has no data model. v2 has a schema, a per-tenant isolation strategy, an audit log, and an outbox. The schema is small (8 upper-core tables + 4 runtime tables + per-vertical) and is the bare minimum to support a durable, multi-tenant, side-effecting system.

---

## 13. Runtime substrate (Temporal + LangGraph)

Per CRMArena-Pro's reliability gap and the production-agent literature (cordum.io, zylos.ai 2026):

- **Temporal** for the outer workflow (durability, retries, multi-day waits, compensations).
- **LangGraph** for the inner agent reasoning (cyclical control flow, state machine, conditional routing).
- **Every Temporal activity** corresponds to a pipeline stage. Activity failures trigger Temporal's policy-driven retry.
- **Every LangGraph node** corresponds to a sub-step within a stage. Checkpoint per node to Postgres.
- **Outbox is the bridge** between LangGraph's "I decided to act" and Temporal's "I'm going to call the external system."

This is the layered durability pattern that's emerging as the production default in 2026.

---

## 14. Open questions for the junior

These are the design choices that v2 makes a recommendation on, but the junior should weigh in:

1. **Vertical priority for v1 release.** Recommendation: real estate (lower regulatory bar than clinic, higher willingness-to-pay than generic sales). Alternative: clinic (higher revenue, harder).
2. **Build vs buy the runtime substrate.** Recommendation: Temporal Cloud + LangGraph (managed). Alternative: Temporal OSS + custom checkpointer. The first removes a class of operational burden; the second is cheaper at scale.
3. **First saved-state authoring path.** Recommendation: Discovery loop produces a starter snapshot, owner reviews and edits in the Web Console. Alternative: pure questionnaire, no discovery.
4. **Approval default for `send_external`.** Recommendation: `requires_approval` by default, owner-configurable to `auto` after 30 days of clean track record. Alternative: always require approval.
5. **Outbox drainer concurrency.** Recommendation: separate drainer worker with bounded concurrency per tenant. Alternative: inline dispatch (faster, but loses durability guarantee).
6. **What to do about CustomNerd.** Recommendation: publish v2 as a separate artifact with explicit cross-references to CustomNerd in the dissertation chapter, not as a fork. Alternative: include Operations Nerd as a CustomNerd case study.

The junior's response to these six questions, plus any pushback on the ontology or the HITL gate, should drive v3.

---

## 15. Summary: what v1 got right, what v2 adds

**v1 got right:**
- Architectural thesis (config-first, shared ontology, vertical adapter).
- Naming of the four universal SMB functions.
- The CustomNerd analog framing.
- The walkthrough example (real-estate call log).
- The Stage 5 (compliance) and Stage 6 (action) are the right places to focus.

**v2 adds (engineering substrate):**
- Three planes (onboarding / runtime / discovery), instead of one.
- Two-layer ontology (8 upper + per-vertical lower), instead of 4 primitives.
- Real data model with RLS multi-tenancy.
- Durable execution substrate (Temporal + LangGraph).
- Transactional outbox + saga for side effects, with idempotency four-tuple.
- Explicit HITL subsystem with action classification, three-signal confidence, approval queue.
- Pluggable compliance engine (rules + LLM-as-judge + HITL).
- Snapshot model à la Replit (versioned, immutable, replayable, hash-checked).
- Discovery loop (process mining on the event log).
- Four-layer evaluation plan (benchmarks, monitoring, feedback, business KPIs).
- Failure-mode catalog with v2-specific guards.
- Runtime topology and deployment story.

**What's still to do (v3 and beyond):**
- Pick first vertical and produce a real lower-core ontology for it.
- Build the CRMArena-style benchmark for that vertical.
- Build the Web Console (UI design + Figma).
- Decide Temporal Cloud vs OSS.
- Pilot with 3–5 businesses in the chosen vertical.

---

*Prepared by:* Senior researcher, 2026-07-25
*For:* the junior researcher (HLD review), Professor Dennis Sasha (proposal)
*Sources consulted:* CRMArena / CRMArena-Pro (Salesforce AI Research 2024–2025), O-CREAM-v2 (Cairns et al. 2010), CURIE-O (Sales et al. 2021), PMAx / pm4aa (process-mining + LLM, 2026), Agent Behavior Mining (arXiv 2606.20669), Salesforce HITL best practices (2026), Fiddler / gravity.fast / armalo 2026 practitioner studies, Perae.ai idempotency research, LangGraph 1.0 + Temporal integration docs, Replit Snapshot Engine architecture, Postgres RLS production patterns.
