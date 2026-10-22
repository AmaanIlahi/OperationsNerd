-- record_links prototype. Applied on top of operations_nerd/db/schema.sql.
-- Needs PRAGMA foreign_keys = ON on every connection, as the app already sets.

-- Parent key for the composite foreign keys below. id alone is already unique,
-- so this index can never fail to build on existing data.
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_business_id_type
    ON entities(business_id, id, entity_type);

-- One row per link value: record from_id points at record to_id under link_key.
-- Every column is NOT NULL because SQLite skips a foreign key check when any
-- column of the key is NULL.
CREATE TABLE IF NOT EXISTS record_links (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id  INTEGER NOT NULL,
    link_key     TEXT NOT NULL,
    cardinality  TEXT NOT NULL CHECK (cardinality IN ('many_to_one', 'one_to_one')),
    from_id      INTEGER NOT NULL,
    from_type    TEXT NOT NULL,
    to_id        INTEGER NOT NULL,
    to_type      TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (business_id, link_key, from_id),
    FOREIGN KEY (business_id, from_id, from_type) REFERENCES entities(business_id, id, entity_type),
    FOREIGN KEY (business_id, to_id, to_type) REFERENCES entities(business_id, id, entity_type)
);

-- one_to_one: a target record is held by at most one source per link.
CREATE UNIQUE INDEX IF NOT EXISTS idx_record_links_one_to_one
    ON record_links(business_id, link_key, to_id) WHERE cardinality = 'one_to_one';

-- Child-side indexes for the foreign keys, so deleting or rekeying an entity
-- does not scan record_links. The "to" one also serves "who links to X".
CREATE INDEX IF NOT EXISTS idx_record_links_from ON record_links(business_id, from_id, from_type);
CREATE INDEX IF NOT EXISTS idx_record_links_to ON record_links(business_id, to_id, to_type);

-- Every row of one link (business_id, link_key) must agree on cardinality,
-- from_type and to_type. Existing rows already agree with each other, so
-- comparing against any one of them is enough and stays an index lookup.
-- `id IS NOT NEW.id` rather than `<>`: on INSERT, NEW.id can still be NULL.
CREATE TRIGGER IF NOT EXISTS record_links_consistent_insert
BEFORE INSERT ON record_links
WHEN EXISTS (
    SELECT 1 FROM (
        SELECT cardinality, from_type, to_type FROM record_links
        WHERE business_id = NEW.business_id AND link_key = NEW.link_key AND id IS NOT NEW.id
        LIMIT 1
    ) r
    WHERE r.cardinality <> NEW.cardinality OR r.from_type <> NEW.from_type OR r.to_type <> NEW.to_type
)
BEGIN
    SELECT RAISE(ABORT, 'record_links: cardinality or types differ from this link''s existing rows');
END;

CREATE TRIGGER IF NOT EXISTS record_links_consistent_update
BEFORE UPDATE OF business_id, link_key, cardinality, from_type, to_type ON record_links
WHEN EXISTS (
    SELECT 1 FROM (
        SELECT cardinality, from_type, to_type FROM record_links
        WHERE business_id = NEW.business_id AND link_key = NEW.link_key AND id IS NOT NEW.id
        LIMIT 1
    ) r
    WHERE r.cardinality <> NEW.cardinality OR r.from_type <> NEW.from_type OR r.to_type <> NEW.to_type
)
BEGIN
    SELECT RAISE(ABORT, 'record_links: cardinality or types differ from this link''s existing rows');
END;
