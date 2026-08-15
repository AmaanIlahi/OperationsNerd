-- Operations Nerd core schema
-- These tables are fixed and vertical-agnostic. No Config Pack ever adds a table
-- or a new foreign key relationship here. Vertical-specific fields live in the
-- entity_attributes EAV table below, keyed by (entity_type, entity_id, attribute_name).
-- Callers work with plain dicts and never touch EAV rows directly -- the
-- set_entity_attributes / get_entity_attributes helpers in db.py merge attrs
-- in and out of the contact / follow_up dicts.
--
-- Falsifiability note: if building a vertical ever requires a new table or a new
-- relationship (not just a new field), that is a true break in the core thesis,
-- assuming it survives a correctly rewritten Config Pack.

PRAGMA foreign_keys = ON;

-- One row per business using the system. Created during setup questionnaire.
CREATE TABLE IF NOT EXISTS businesses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    industry_pack   TEXT NOT NULL,         -- e.g. 'realestate' -- which Config Pack is loaded
    pack_version    TEXT NOT NULL,
    settings_json   TEXT NOT NULL DEFAULT '{}',  -- questionnaire answers, shape defined by the pack
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Universal contact record. Every vertical has "people/organizations you deal with"
-- even if what they're called differs (lead, client, patient, member, guest).
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    name            TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',  -- active | inactive, universal enough to stay core
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Universal follow-up / task record tied to a contact.
CREATE TABLE IF NOT EXISTS follow_ups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    contact_id      INTEGER NOT NULL REFERENCES contacts(id),
    type            TEXT NOT NULL,          -- e.g. call, email, meeting -- values come from the pack's event schema
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | dismissed
    due_date        TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Generalized EAV table for vertical-specific attributes on any core entity.
-- Suggested by Prof. Shasha, based on the ecommerce pattern for heterogeneous
-- item attributes (e.g. watches care about wrist diameter, pants about inseam,
-- all items care about price). One table serves every entity_type, so adding
-- a new core entity type in the future still doesn't require a new table.
-- Values are stored untyped (TEXT) for v1; numeric comparisons can use
-- CAST(value AS REAL) if a capability comparison later requires it.
CREATE TABLE IF NOT EXISTS entity_attributes (
    entity_type     TEXT NOT NULL,      -- 'contact' | 'follow_up'
    entity_id       INTEGER NOT NULL,
    attribute_name  TEXT NOT NULL,
    value           TEXT NOT NULL,
    PRIMARY KEY (entity_type, entity_id, attribute_name)
);

CREATE INDEX IF NOT EXISTS idx_entity_attributes_lookup ON entity_attributes(entity_type, attribute_name, value);

-- Raw inbound events (email, call note, form submission) before the pipeline
-- turns them into a structured record + drafted action.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    contact_id      INTEGER REFERENCES contacts(id),   -- nullable: may not be resolved yet
    source          TEXT NOT NULL,           -- email | call_note | form
    raw_content     TEXT NOT NULL,
    parsed_data     TEXT NOT NULL DEFAULT '{}',  -- structured output of the pipeline's LLM call
    processed       INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
    received_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Drafted action produced by the event pipeline, sitting in front of Human Approval.
CREATE TABLE IF NOT EXISTS drafted_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES events(id),
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    contact_id      INTEGER REFERENCES contacts(id),
    action_type     TEXT NOT NULL,           -- e.g. send_follow_up_email -- pack-defined vocabulary
    payload         TEXT NOT NULL DEFAULT '{}',  -- drafted content (subject, body, etc.)
    status          TEXT NOT NULL DEFAULT 'pending_approval',
                    -- pending_approval | approved | rejected | auto_approved | sent
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT
);

-- Per-business, per-action-type approval policy. Default is to ask;
-- an owner can flip specific low-stakes action types to automatic.
CREATE TABLE IF NOT EXISTS approval_policies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id     INTEGER NOT NULL REFERENCES businesses(id),
    action_type     TEXT NOT NULL,
    auto_approve    INTEGER NOT NULL DEFAULT 0,  -- 0/1 boolean
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(business_id, action_type)
);

CREATE INDEX IF NOT EXISTS idx_contacts_business ON contacts(business_id);
CREATE INDEX IF NOT EXISTS idx_follow_ups_business ON follow_ups(business_id);
CREATE INDEX IF NOT EXISTS idx_follow_ups_contact ON follow_ups(contact_id);
CREATE INDEX IF NOT EXISTS idx_events_business ON events(business_id);
CREATE INDEX IF NOT EXISTS idx_drafted_actions_business ON drafted_actions(business_id);
