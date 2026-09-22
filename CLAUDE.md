# Claude Code brief: Operations Nerd, warehouse / goods extension

Read this whole file before running anything. It carries the context for work
already done, so you can reproduce it locally and continue from it.

---

## 0. HARD CONSTRAINTS. Read first, no exceptions.

This is a shared research repo. A teammate, Amaan, owns the core and four
working verticals. His code must not change.

**Never modify, reformat, refactor, or "improve" any of these:**

```
operations_nerd/db/schema.sql
operations_nerd/db/db.py
operations_nerd/packs/loader.py
operations_nerd/packs/realestate/*
operations_nerd/packs/healthclub/*
operations_nerd/packs/**          (every existing pack)
operations_nerd/pipeline/*
operations_nerd/api/*
```

**You may only create files in:**

```
operations_nerd/experiments/      (new directory)
prototype/                        (new directory, repo root)
```

Rules that follow from that:

1. Work on a branch. Never commit to `main`.
2. Treat the core as a read-only library. Import from it, call it, inspect it.
   Never patch it, monkey-patch it, or "fix" something that looks wrong. If
   something looks like a bug, write it in the notes, do not change it.
3. No migrations. No `ALTER TABLE` against any real database file.
4. All experiments use a temp directory or an in-memory SQLite database.
   Never touch a `.db` file that already exists in the repo.
5. Do not add, remove, or pin dependencies. `pydantic` and `pyyaml` are
   already in `requirements.txt` and are all that is needed.
6. If a task seems to require editing core code, stop and say so instead of
   doing it. That is a finding, not an obstacle.

The whole research result depends on the experiments running against the
unmodified core. Changing core code to make something pass destroys the result.

---

## 1. Project context

Operations Nerd tests one claim: a single config-first system can serve many
industries, with everything industry-specific in a swappable Config Pack and
zero vertical-specific code in the core.

`schema.sql` states the falsifiability rule in its own comments: needing a new
table or a new relationship, as opposed to a new field, is a **true break** in
the thesis, *assuming it survives a correctly rewritten Config Pack.*

Findings are classified as: (a) pack-authoring gap, (b) implementation bug, or
(c) true break. Only (c) counts as evidence against the claim.

Amaan has proved the claim for relationship management and outreach tracking
across four verticals: real estate, health clubs, restaurants, clinics. All
four are service businesses where a contact maps to a follow-up roughly one to
one. Zero true breaks.

My assignment from Prof. Shasha is the goods side, scoped to a warehouse doing
receiving and shipping. Pre-planning comes later and is out of scope.

### What the core actually provides

Two hardcoded entity types, `contacts` and `follow_ups`, plus an EAV table.

- `contacts`: `id, business_id, name, email, phone, status`. One FK, to `businesses`.
- `follow_ups`: `id, business_id, contact_id, type, status, ...`. One FK, to `contacts`.
- `entity_attributes`: PK `(entity_type, entity_id, attribute_name)`, `value` is
  untyped TEXT, JSON-encoded by the helper. One value per attribute name.
- A `Pack` accepts exactly: `id, version, display_name, questions,
  event_schemas, prompts, approval_defaults`.
- `EventSchema.extract_fields` is typed `list[str]`.
- `drafted_actions`: `action_type` is a free string, `payload` is free JSON.

Helpers in `db.py` used below: `init_db`, `get_conn`, `create_business`,
`create_contact`, `get_contact`, `create_follow_up`, `list_follow_ups`,
`set_entity_attributes`, `find_entity_ids_by_attribute`, `create_event`,
`create_drafted_action`, `get_drafted_action`, `set_approval_policy`,
`is_auto_approved`.

---

## 2. Phase one: the encoding experiment (done, reproduce locally)

### What it is

A script that tries to build a warehouse using **only** what the core provides
today, with zero core changes. The logic: anything this script cannot do, a
Config Pack cannot do either. This is the "correctly rewritten Config Pack"
attempt that the schema's rule demands before anything can be called a break.

Location: `operations_nerd/experiments/warehouse_ugly_encoding.py`
(plus an empty `operations_nerd/experiments/__init__.py`)

### How it works

Each step attempts one encoding and records a verdict of PASS, DEGRADED or
FAIL, along with what was tried, what happened, and which line of the core
blocked it. Results print to stdout and are written to
`warehouse_failure_notes.json` next to the script.

The steps, in order:

1. Partner as a contact with a `role` attribute
2. Carrier as a contact with a `role` attribute
3. Item as a contact with `role='item'`
4. Purchase order header as a contact
5. Order lines as `follow_ups` pointing at the order, qty and price as attributes
6. Order line also referencing an item
7. Workaround: all lines stuffed into one JSON attribute on the order
8. Shipment referencing an order, a partner and a carrier at once
9. Partial receipt (order 500, receive 60 then 440)
10. Pack declaring an event with nested line items
11. Pack declaring a new record type
12. Approval gate wrapping a state change instead of a message

### Running it

```bash
git checkout -b jasmeet/warehouse-encoding-experiment
mkdir -p operations_nerd/experiments
touch operations_nerd/experiments/__init__.py
# place warehouse_ugly_encoding.py in that directory
python -m operations_nerd.experiments.warehouse_ugly_encoding
```

Expected: **4 pass, 3 degraded, 5 fail.**

If your numbers differ, that difference is the interesting thing. Report it,
do not adjust the script to match.

---

## 3. Phase two: the findings

Written up in `WAREHOUSE_FAILURE_NOTES.md`.

### Passed cleanly

- **Partner and carrier as role contacts.** A supplier is an organisation you
  deal with, and `role` is single-valued. Exactly what contacts and EAV are for.
- **Order lines as follow_ups.** This contradicts the original prediction.
  `follow_ups` already gives a one-to-many, and one line's quantity and price
  are single values. **Repeated rows are not the break.**
- **Approval gate wrapping a state change.** Also contradicts the original
  prediction. `action_type` is a free string and `payload` is free JSON, so
  `post_inventory_receipt` gates exactly like an outbound email. **The approval
  layer is not part of the problem.**

### Degraded

- **Item as a contact.** Works mechanically but it is the first record that is
  not "someone you deal with". A bolt inherits email, phone and relationship
  status. Modelling smell, not a technical failure.
- **Order header as a contact.** `supplier_id` is a bare integer in a TEXT
  attribute. Not a foreign key, not enforced, nothing cascades.
- **Lines as one JSON blob.** `set_entity_attributes` json-encodes values so a
  list does round-trip. But `find_entity_ids_by_attribute` compares the whole
  encoded string for equality, so "which orders contain BOLT-M6" is
  unanswerable. Stored but not queryable, not joinable, not individually
  updatable.

### The actual breaks

**The break is the second reference, not the repeated rows.**

- An order line must point at both its order and its item. `follow_ups` has one
  FK and the order owns it. The item degrades to an untyped integer: writing
  `item_id=999999` against a contact that does not exist **succeeded**. No
  integrity, no cascade, no ON DELETE.
- A shipment must reference an order, a partner and a carrier simultaneously.
  `contacts` has one FK, `follow_ups` has one FK. Three references cannot be
  expressed in a schema offering one.
- Partial receipt compounds both.

**And a separate break upstream of the database:**

- `EventSchema.extract_fields` is typed `list[str]`. A nested `{"lines": [...]}`
  entry is rejected by the loader's own validation before the database is
  involved. The pipeline cannot parse a purchase order email at all.
- The `Pack` model has no key for entities, record types or relationships. A
  pack can introduce vocabulary but not nouns.

### Classification

Not a pack-authoring gap (no field exists that a better author could have
used). Not an implementation bug (everything behaves as documented).
**True break on two counts: a new relationship, and a new record type.**

Qualification kept in the writeup: this is one vertical. It shows the current
core cannot express goods. It does not by itself prove goods in general need a
core change. That needs a second goods vertical.

---

## 4. Phase three: the generic solution

Specced in `SPEC_RECORDS_AND_LINKS.md`, prototyped in
`prototype/prototype_records_links.py`.

### The decision behind it

Do **not** add `orders`, `shipments`, `items`, `carriers` as tables. That
solves the warehouse and leaves the next goods vertical in the same position.
Add one generic mechanism instead. This was Harsh's proposal and he has
reviewed and agreed with the classification.

Amaan's instruction: build this **separately** so it does not clash with his
work. It will be reconciled later once the structure is proven.

**The discipline that keeps it mergeable:** build it as a drop-in, not a
parallel system. Same table names, reuse `entity_attributes` exactly as it is
rather than inventing new field storage, same YAML pack shape, nothing
vertical-specific inside the four tables. The prototype therefore reproduces
`contacts` and `entity_attributes` unchanged rather than redesigning them.

### Four tables, nothing existing modified

| Table | Holds |
|---|---|
| `record_types` | which nouns this business has, from the pack |
| `link_types` | which references are legal, and their constraints |
| `records` | record identity: id, business, type, display name, status |
| `record_links` | the references themselves |

Fields are **not** part of this. Records reuse `entity_attributes` with
`entity_type='record'`, unchanged. `contacts` and `follow_ups` sit beside this
untouched, which is why a pack with no `record_types` block is unaffected.

### The new pack key

```yaml
record_types:
  - name: item
    fields: [sku, unit, reorder_point]
  - name: order
    fields: [ordered_on, status]
    links:
      - {name: supplier, to: contact, required: true}
      - {name: carrier,  to: contact, required: false}
  - name: order_line
    fields: [qty_ordered, unit_price]
    links:
      - {name: order, to: order, required: true, on_delete: cascade}
      - {name: item,  to: item,  required: true, on_delete: restrict}
  - name: shipment
    fields: [direction, moved_on, tracking_ref]
    links:
      - {name: order,   to: order,   required: true}
      - {name: partner, to: contact, required: true}
      - {name: carrier, to: contact, required: false}
```

`to:` takes either `contact`, meaning the existing table, or the name of
another record type. That one distinction lets goods records point at people
records without duplicating the contact concept.

### How invalid references are rejected

`record_links` has two nullable target columns with a CHECK that exactly one is
set, so both targets are real foreign keys rather than one untyped polymorphic
column:

```sql
to_record_id  INTEGER REFERENCES records(id),
to_contact_id INTEGER REFERENCES contacts(id),
CHECK ((to_record_id IS NULL) != (to_contact_id IS NULL))
```

Semantic checks live in **one** trigger with three ordered statements.
Important: separate triggers do not work, because SQLite does not define firing
order between them, and the first version of this had all three failures
reporting the same misleading message.

### Verified prototype output

| Attempt | Result |
|---|---|
| order → partner | ok |
| order_line → order AND item | ok |
| shipment → order + partner + carrier | ok |
| `item_id=999999` | rejected: link target record does not exist |
| item link pointing at an order record | rejected: link target has the wrong record type |
| undeclared link name | rejected: undeclared link name for this record type |
| order_line with no item link | rejected: missing required link(s): ['item'] |
| record of an undeclared type | rejected: FOREIGN KEY constraint failed |
| deleting an item a line references | rejected: FOREIGN KEY constraint failed |
| query: which orders contain BOLT-M6 | `[('PO-1001', '500')]` |
| existing contacts / EAV path | unaffected |

Run with:

```bash
mkdir -p prototype
# place prototype_records_links.py there
python prototype/prototype_records_links.py
```

Expected final line: `ALL CHECKS BEHAVED AS SPECIFIED`.

### What this does to the thesis

Original claim: new industries need new packs, not new code. Warehouse shows
that is false as stated for goods.

Proposed replacement: config covers fields, vocabulary and relationships; the
core grows one generic mechanism when a structurally new shape appears, and
that mechanism is not vertical-specific. Warehouse cost one extension. The
claim is that the next goods vertical costs zero.

**That is what can now fail.** If a second goods vertical needs another core
change, the weaker thesis fails too.

### Still open, deliberately not solved here

- `extract_fields` is a separate break and needs its own change. Even with
  records and links in place, a pack still cannot describe an inbound purchase
  order containing line items.
- Quantity and inventory balance are out of scope (Prof. Shasha scoped this to
  receiving and shipping). Derivable by summing shipment lines.
- No migration of `contacts` / `follow_ups` into record types. Beside, not
  instead.

---

## 5. What to do next

In order:

1. Reproduce both scripts locally and confirm the expected counts.
2. Commit to the branch. Do not open a PR against `main` yet.
3. Build out the prototype toward a live demo. The demo is five beats and
   nothing more: show the pack YAML, create an order with a line pointing at
   both order and item, create a shipment with three references, let a bad
   reference get rejected on screen, run the "which orders contain this item"
   query. Stop there.
4. Ask Amaan whether there is a test suite covering his four packs. That is the
   safety net for the eventual merge.

Do not build inventory levels, reorder thresholds, the event pipeline
integration, or a UI. Scope discipline is part of what is being demonstrated.

---

## 6. Reminders

- Never edit core files. If something requires it, that is a finding to write
  down, not a change to make.
- Never adjust an experiment so it produces the expected result. A different
  result is data.
- Every claim in the notes must be produced by a script that runs, not asserted
  in prose. That is the standard the rest of this project is held to.
