Superseded by agentic_crm_architecture.md.

# Chat Onboarding — Architecture

Status: approved plan, not yet built. Source of truth for the chat-onboarding module.

## Goal

A business owner describes their business in plain language in a chat. The agent builds a working CRM setup from that conversation: the business record plus initial Accounts, Contacts, and Leads. This replaces the setup work businesses currently pay a third-party agency to do.

It is a research prototype. It does not need to be exhaustive. It must work end to end for Health Club, then run on Real Estate with no code changes.

## Hard rules

1. **The existing app is not touched.** No edits to `questionnaire/`, `pipeline/`, `state/`, `frontend/index.html`, or existing tables. `main.py` gets additive lines only. A `git diff` must prove this.
2. **No vertical-specific code.** The agent has zero hardcoded knowledge of what an Account or Lead looks like for any industry. Entity types and fields come from the loaded Config Pack, same principle as the Event Pipeline's `action_type` enum.
3. **Propose, then confirm.** The agent never writes to the database on extraction. It proposes records, the owner confirms, then they persist.
4. **Additive schema only.** New tables and nullable columns. No changes to existing columns or rows.

## Module layout

```
operations_nerd/
  chat_onboarding/
    __init__.py
    routes.py          # router, mounted in main.py
    service.py         # turn handling: prompt build, LLM call, parse, validate
    entity_schema.py   # EntitySchema model + extraction-schema builder
    auth.py            # invite-code dependency -> owner_key
  frontend_chat/
    index.html         # landing chat page, mounted at /chat
  packs/
    healthclub/entity_schemas.yaml   # new
    realestate/entity_schemas.yaml   # new
```

`frontend_chat/` is a separate page, so it does not conflict with the PR #9 rewrite of `frontend/index.html`.

## Data model (additive)

```sql
CREATE TABLE IF NOT EXISTS entities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL REFERENCES businesses(id),
    entity_type  TEXT NOT NULL,       -- crm_account | crm_contact | crm_lead
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_entities_business_type
    ON entities(business_id, entity_type);

ALTER TABLE businesses ADD COLUMN owner_key TEXT;   -- NULL for questionnaire-created businesses
```

- `entity_attributes` is reused unchanged for entity fields.
- New `entity_type` values are namespaced (`crm_*`) so they never collide with the existing `contact` / `follow_up` values.
- The existing `contacts` table is untouched.
- `PRAGMA journal_mode=WAL` is set in `init_db`.

## Config Pack addition: `entity_schemas.yaml`

Optional file, parallel to `event_schemas.yaml`. Packs without it still load.

```yaml
entities:
  - entity_type: crm_account
    label: Account
    fields: [name, industry, phone, email]
    required: [name]
  - entity_type: crm_contact
    label: Contact
    fields: [first_name, last_name, email, phone, account_name]
    required: [first_name]
  - entity_type: crm_lead
    label: Lead
    fields: [name, email, phone, source, status]
    required: [name]
```

Business-level fields (hours, trial days, etc.) still come from the pack's existing `questionnaire.yaml`. The agent collects them conversationally instead of through the form, and validates them with the existing `validate_answers()`.

## Turn flow

```
client sends: { pack_id, business_id?, messages: [...full history] }
        |
        v
service builds prompt from:
  - questionnaire.yaml questions (business fields still missing)
  - entity_schemas.yaml (allowed entity types + fields)
        |
        v
one call_llm() call, JSON output constrained to:
  {
    reply: str,                         # what the agent says next
    business_fields: { key: value },    # answers found so far
    proposed_entities: [ { entity_type, fields } ],   # max 5 per turn
    missing: [ question_id ]            # what it still needs to ask
  }
        |
        v
validate: business_fields via validate_answers();
          entities: entity_type in pack enum, required fields present
        |
        v
return proposal to client (nothing persisted yet)
        |
        v
owner clicks Confirm -> POST /chat/confirm -> persist
```

Invalid LLM output (bad JSON, unknown entity type, missing required field) is dropped from the proposal and surfaced in the response, never persisted.

## Endpoints (all under `/chat`, all require `X-Invite-Code`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/chat/packs` | list packs that have `entity_schemas.yaml` |
| POST | `/chat/turn` | run one conversation turn, return proposal |
| POST | `/chat/confirm` | persist a confirmed proposal (create or update business + entities) |
| GET | `/chat/businesses/{id}` | business + its entities, scoped to owner |
| PATCH | `/chat/businesses/{id}` | merge into `settings_json` |
| PATCH | `/chat/entities/{id}` | merge entity attributes |

## Access control

- `INVITE_CODES` env var, comma-separated.
- `auth.py` dependency validates the header and resolves it to `owner_key` (hash of the code).
- Every `/chat` read and write filters by `owner_key`. Tester A cannot see or edit tester B's data, even with a guessed ID.
- Not real auth. No real customer data.

## Frontend: `/chat`

DietNerd-style landing page.

- Center: chat. Empty state shows a pack selector and starter chips ("Tell me about your gym", "Add my first member").
- Right panel: **What I'll create**. Shows the current proposal: business fields, then entities grouped by type, each marked valid or with the missing field named. Confirm button persists it.
- After confirm, the panel shows what was saved, with record IDs.
- Invite code entered once, kept in memory for the session.
- Reuses the safe DOM-builder pattern from `frontend/index.html` (no `innerHTML`).

## Testing

- `tests/test_chat_onboarding_smoke.py` with the LLM call stubbed:
  - turn returns a valid proposal and persists nothing
  - confirm persists business + entities
  - unknown entity type and missing required field are rejected
  - owner isolation: second invite code cannot read or patch first code's business
  - pack without `entity_schemas.yaml` still loads
- Full existing suite must pass unchanged.

## Editing running businesses

| Edit | Example | How | Cost |
|---|---|---|---|
| Change a business's data | Update a lead's status, fix an email | `PATCH /chat/businesses/{id}`, `PATCH /chat/entities/{id}`, owner-scoped | Zero code |
| Add a field for a whole vertical | Every gym's leads get "preferred class time" | Add it to that pack's `entity_schemas.yaml` | One YAML line. No migration: fields live in `entity_attributes`, so old records simply lack the value |
| Add a field for one business only | One gym wants "locker number" | **Not supported yet.** See Out of scope | — |

Safety notes:
- **Data isolation.** Every `/chat` route filters by `owner_key`. Businesses created by the old questionnaire have `owner_key = NULL` and those routes are unscoped, so external testers use `/chat` only.
- **Pack edits hit every business on that vertical at once.** Adding an optional field is safe. Adding a required field makes existing records incomplete. The loader should warn when a required field is added to a pack that already has businesses.

## Out of scope for now (planned next)

1. **Per-business overlay.** Custom fields for a single business, stored against that business and merged over the pack's `entity_schemas.yaml` at read time. The agent offers them for that business only. This is the "add a field to one running business" case and the next research test: can a one-off customization be added without code, without touching the pack, and without affecting other businesses on the same vertical.
2. **Config versioning.** Record the pack version (e.g. a content hash of the pack files) on each business and entity when it is created or updated. Lets you tell old records from new ones after a pack edit, and is the foundation for the event replay / approval audit trail idea.
3. **Scoping the old questionnaire routes by `owner_key`.**
4. **Deployment.** Out of scope for this build. Local demo first.

## Measurement (for the research log)

Record at the end:
- Files and lines changed, split into (a) core engine, (b) per-pack config.
- `git diff --stat` on existing files, which must show only additive lines in `main.py`, `db/schema.sql`, `db/db.py`, `packs/loader.py`.
- Lines needed to add Real Estate once Health Club works. Target: `entity_schemas.yaml` only.
