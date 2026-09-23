# Spec: generic records and links

**Status:** proposal for review by Harsh, then Amaan and Prof. Shasha
**Follows from:** `WAREHOUSE_FAILURE_NOTES.md` (true break, two counts)
**Prototype:** `prototype_records_links.py` — every claim below is executable
**Principle:** one generic mechanism, once. No vertical-specific code in the core.

---

## What this adds

Four tables and one new pack key. Nothing existing changes.

| Table | Holds |
|---|---|
| `record_types` | which nouns this business has, from the pack |
| `link_types` | which references are legal, and under what constraints |
| `records` | record identity: id, business, type, display name, status |
| `record_links` | the references themselves |

Fields are **not** part of this. `entity_attributes` already stores them and is
already keyed on `(entity_type, entity_id, attribute_name)`, so records reuse it
with `entity_type='record'` unchanged. The schema comment anticipated new
entity types; this uses that, rather than cutting across it.

`contacts` and `follow_ups` are untouched and sit beside this. A pack with no
`record_types` block registers nothing, which is why the four existing packs are
unaffected. The prototype checks this explicitly.

---

## How a pack declares record types and link constraints

New top-level `record_types` key. Each type names its fields and its outgoing
links; a link states its target, whether it is required, and its delete rule.

```yaml
record_types:
  - name: item
    label: Item
    fields: [sku, unit, reorder_point]

  - name: order
    label: Purchase Order
    fields: [ordered_on, status]
    links:
      - name: supplier
        to: contact          # reuses the existing contacts table
        required: true
      - name: carrier
        to: contact
        required: false

  - name: order_line
    fields: [qty_ordered, unit_price]
    links:
      - name: order
        to: order
        required: true
        on_delete: cascade   # delete the order, its lines go too
      - name: item
        to: item
        required: true
        on_delete: restrict  # cannot delete an item a line still references

  - name: shipment
    fields: [direction, moved_on, tracking_ref]
    links:
      - name: order
        to: order
        required: true
      - name: partner
        to: contact
        required: true
      - name: carrier
        to: contact
        required: false
```

`to:` takes either `contact`, meaning the existing table, or the name of
another record type in the same pack. That one distinction is what lets goods
records point at people records without duplicating the contact concept.

The loader is small: walk `record_types`, insert a row per type, then a row per
link. That is the entire implementation of the pack side.

---

## The three examples

### order → partner

```python
order = create_record(conn, bid, "order", "PO-1001",
                      fields={"ordered_on": "2026-09-10", "status": "open"},
                      links={"supplier": ("contact", supplier_id)})
```

The supplier stays a contact with a role, exactly as it already works. What is
new is that the order is a first-class record pointing at it, with a real
foreign key rather than an integer in a TEXT column.

### order line → order + item

The case that broke before, because `follow_ups.contact_id` was already spent.

```python
line = create_record(conn, bid, "order_line",
                     fields={"qty_ordered": 500, "unit_price": 0.12},
                     links={"order": ("record", order),
                            "item":  ("record", bolt)})
```

Both references are real, enforced, and independently typed. And the query that
was unanswerable is now a join. Prototype output:

```
orders containing BOLT-M6: [('PO-1001', '500')]
```

### shipment → order + partner + carrier

Three simultaneous references, which no encoding in the current core could
express.

```python
create_record(conn, bid, "shipment", "SHP-5001",
              fields={"direction": "inbound", "moved_on": "2026-09-14"},
              links={"order":   ("record", order),
                     "partner": ("contact", supplier_id),
                     "carrier": ("contact", carrier_id)})
```

Note the shipment points at records and contacts in the same breath. That mix
is the thing the single-FK design made impossible.

---

## How invalid references are rejected

`item_id=999999` succeeded before, because it was an integer in an untyped
column. Under this design it is rejected four different ways depending on what
is actually wrong, and each reports its own cause rather than one generic
message.

**Structural, in the table definition.** `record_links` has two nullable target
columns with a CHECK that exactly one is set:

```sql
to_record_id  INTEGER REFERENCES records(id),
to_contact_id INTEGER REFERENCES contacts(id),
CHECK ((to_record_id IS NULL) != (to_contact_id IS NULL))
```

Two real foreign keys instead of one polymorphic untyped column. `on_delete`
from the pack maps onto real FK behaviour, so `restrict` actually restricts.

**Semantic, in one trigger with three ordered checks.** Separate triggers will
not do, because SQLite does not define firing order between them, so all three
live in one body where statements run in sequence.

Verified prototype output:

| Attempt | Rejected with |
|---|---|
| `item_id=999999` | `link target record does not exist` |
| item link pointing at an `order` record | `link target has the wrong record type` |
| link name the pack never declared | `undeclared link name for this record type` |
| `order_line` created with no `item` link | `missing required link(s): ['item']` |
| record of a type the pack never declared | `FOREIGN KEY constraint failed` |
| deleting an item a line still references | `FOREIGN KEY constraint failed` |

The pack declares intent; the database enforces it. A pack author cannot write
a dangling reference even by mistake, which is the property the current EAV
encoding cannot offer at all.

---

## Still open, and not solved by this

**`extract_fields` is a separate break.** Even with this in place,
`EventSchema.extract_fields` is typed `list[str]`, so a pack still cannot
describe an inbound purchase order containing line items. The pipeline cannot
parse the event whose result these tables would store. It needs its own change
and should be specced separately rather than folded in here.

**Quantity and balance are deliberately out.** Inventory on hand is derivable by
summing shipment lines, and Prof. Shasha scoped this to receiving and shipping.
Pre-planning will likely force the question; this design does not prejudge it.

**Migration is not proposed.** `contacts` and `follow_ups` could eventually be
expressed as record types, which would be cleaner. Not now — beside, not
instead, keeps Amaan's four packs working untouched.

---

## What this does to the thesis

Honestly, it weakens the original claim and replaces it with a sharper one.

Original: new industries need new packs, not new code.

What warehouse showed: false as stated, for goods. No pack could express it.

Proposed replacement: **config covers fields, vocabulary and relationships;
the core grows one generic mechanism when a structurally new shape appears, and
that mechanism is not vertical-specific.** Warehouse cost one extension. The
claim is that the next goods vertical costs zero.

**That is the thing that can now fail.** If a second goods vertical needs
another core change, the weaker thesis fails too, and the honest conclusion
becomes that config-first holds only within a structural family. That test is
the obvious next piece of work, and it needs a second goods vertical chosen for
the purpose.
