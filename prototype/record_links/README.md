# record_links prototype

A throwaway proof that the planned `record_links` table can enforce link rules in SQLite against today's schema, and that existing link values can be moved out of `entity_attributes` safely. It does not touch `operations_nerd/` and is not wired into the app.

## What it shows

1. **The database can hold link integrity on its own.** With the migration in [migration.sql](migration.sql) applied on top of `operations_nerd/db/schema.sql`, raw SQL inserts that bypass `records.py` are rejected when they break a link rule. The table below lists each rule and what enforces it:

   | Rule | Enforced by |
   |---|---|
   | Target and source exist, have the declared type and belong to the row's business | Composite FKs `(business_id, from_id, from_type)` and `(business_id, to_id, to_type)` → `entities(business_id, id, entity_type)`, backed by a new unique index on `entities` |
   | A record holds at most one value per link | `UNIQUE(business_id, link_key, from_id)` |
   | A one_to_one target is held by only one record | Partial unique index on `(business_id, link_key, to_id) WHERE cardinality = 'one_to_one'` |
   | Only writable cardinalities are stored | `CHECK (cardinality IN ('many_to_one', 'one_to_one'))` |
   | All rows of one link agree on cardinality, from_type and to_type | `BEFORE INSERT` and `BEFORE UPDATE` triggers |
   | No NULL slips past the FKs | `NOT NULL` on every key column (SQLite skips an FK check when any key column is NULL) |

   I checked that the tests aren't vacuous by removing each protection in turn: every removal makes its matching tests fail.

2. **Existing EAV link values can be checked and moved.** [migrate_links.py](migrate_links.py) has two functions:
   - `precheck()` sorts every `entity_attributes` row into exactly one category and writes nothing. The test runs it on a read-only connection and compares a dump of the database before and after.
   - `migrate()` takes the write lock, reruns the pre-check, and copies the good link rows into `record_links` in one transaction. The EAV rows are left in place.

   Which rows are links, fields or neither comes from each business's spec, never from key prefixes. The categories are:

   | Category | Rows | What happens |
   |---|---|---|
   | `good` | Values under a link in the spec that pass every check | Copied to `record_links` |
   | `bad` | Values under a link in the spec that can't move | Reported with the reason |
   | `undeclared` | Rows whose name is neither a field of the record's entity nor a link in the spec. Also rows on a record whose entity type or business has no spec | Reported with the reason |
   | `fields` | Values of declared fields | Left alone |
   | `other` | Rows whose `entity_type` is not an entity key in any spec, such as the legacy `contact` / `follow_up` rows | Left alone |

   **How a row's business is found:** `entity_attributes` has no `business_id`. A row's business comes from its source record in `entities`. If that record is gone, it falls back to the one spec that declares the entity key; if several specs declare it, the business is unknown and the row is reported as undeclared.

## How to run

From the repo root, with the venv:

```
./venv/Scripts/python.exe -B prototype/record_links/test_record_links.py     # Windows
./venv/bin/python -B prototype/record_links/test_record_links.py             # macOS / Linux
```

Each test gets its own database in a temp directory. `-B` stops Python writing a `__pycache__` folder into this one.

## Results

Run on 2026-10-22 with Python 3.11.9 and SQLite 3.45.1: **24 tests passed**.

**Rejected by the database:**
- a missing target
- a wrong target type
- a wrong source type
- a target or source in another business
- a second value for the same record and link
- a one_to_one target that is already taken
- a cardinality, from_type or to_type mismatch within one link, on insert and on update
- a non-writable cardinality (`one_to_many`)
- a NULL type
- deleting a record that something links to

**Accepted:**
- a valid many_to_one
- two records sharing one many_to_one target
- a valid one_to_one
- a one_to_one target reused after the old row is deleted

**Migration from EAV:** the seed data has 17 `entity_attributes` rows, written the way `records.py` writes them. To show that nothing relies on prefixes, one link key (`converted`) and the second business's keys (`company`, `person`, `employer`) have no `crm_` or `l_` prefix. The pre-check sorted the rows as 6 good, 6 bad, 2 undeclared, 2 fields and 1 other (a legacy `contact` row). It reported these:

```
bad         business 1  crm_contact 6     l_works_at = 9999      target record 9999 does not exist
bad         business 1  crm_contact 7     l_works_at = 9         target record 9 is a crm_lead, the link expects crm_account
bad         business 1  crm_contact 8     l_works_at = 12        target record 12 belongs to another business
bad         business 1  crm_lead 10       converted = 4          one_to_one target 4 is claimed by records 10, 11
bad         business 1  crm_lead 11       converted = 4          one_to_one target 4 is claimed by records 10, 11
bad         business 1  crm_contact 9999  l_works_at = 1         no crm_contact record with id 9999
undeclared  business 1  crm_contact 3     l_deadbeef = 1         not a field of crm_contact or a link in the spec
undeclared  business 1  crm_contact 5     f_industry = "Retail"  not a field of crm_contact or a link in the spec
```

The second undeclared row is a field that the spec declares only on accounts, written on a contact. Fields are matched per entity, not across the whole spec.

The migration then copied exactly the 6 good rows. Every row lands in exactly one category (17 = 6 + 6 + 2 + 2 + 1), and the counts per link match. The good rows include one value under an archived link and one from a business whose keys have no prefixes.

Other tests cover the remaining pre-check reasons:
- a link value on the wrong entity
- a value under a many_to_many link
- a value that isn't a record id
- a record whose entity type isn't in the spec
- a business with no spec
- a gone record whose entity key two specs declare

**The pre-check is needed, not optional.** The 6 bad rows and the undeclared row that holds a record id were forced straight into `record_links`. The database rejected 5 of the 7 and accepted 2:
- the undeclared `l_deadbeef` row
- whichever of the two one_to_one claimants is inserted first

## Known limits

- **The database checks types against real records, not against the spec.** The first row stored for a link key sets that key's cardinality and types. A link key that isn't in the spec, or one whose types disagree with the spec, is accepted as long as the records are real and the row agrees with the key's earlier rows. The app or the pre-check has to compare against the spec.
  - A possible fix for the plan: once the planned `link_types` table exists, a composite FK from `record_links(business_id, link_key, cardinality, from_type, to_type)` to a unique key on `link_types` would enforce the spec in the database and replace the triggers.
- **Deleting a linked record is blocked.** The FKs use SQLite's default (`NO ACTION`). The spec has no `on_delete` or `required` fields yet, and a required link can't be expressed in this table. Both are still open for the plan.
- **A whole link's cardinality or types can't be changed with one UPDATE.** The trigger fires per row, so the first updated row disagrees with the rest. No spec operation changes a link today (only add, archive and restore), but a future one would need delete and reinsert.
- **Archived links are migrated.** Archiving keeps a link's values, and restoring brings them back, so the prototype moves them. The database doesn't know whether a link is archived. Confirm this choice in the plan.
- **Duplicate one_to_one claims move nothing.** Every row in a conflict is reported and none is migrated, so a person has to choose which claim is right.
- **Rows are classified only as well as the spec allows.** A row is treated as spec-driven only if its `entity_type` is an entity key in some spec. That keeps the legacy `contact` / `follow_up` rows out, but only because no spec can declare those keys: `ENTITY_KEY_RE` in `agentic/spec.py` requires `crm_`. If that rule ever loosens, legacy rows could be mistaken for orphaned spec rows.
- **A missing record's business is inferred, not stored.** When a row's source record is gone, the business comes from the spec that declares its entity key. That works because keys are generated per business, but it isn't guaranteed unique; when two specs share a key, the row is reported as undeclared with an unknown business.
- **Orphaned field values are left alone.** A field value whose record is gone lands in `fields`, because this migration only moves links.
- **The pre-check needs the current spec for every business.** Real specs live in `spec_versions.spec_json` and `businesses.current_version`. The prototype passes them in as dicts, and loading them is left to the real migration.
- **Prototype scope only.** It isn't wired into `records.py`. It covers no cutover, dual writes or removal of the EAV rows. It was tested on small data with SQLite 3.45.1 only, and has no performance numbers.
