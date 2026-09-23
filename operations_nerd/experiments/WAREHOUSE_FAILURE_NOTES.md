# Warehouse encoding: failure notes

**Status:** experiment complete, classification proposed
**Scope:** warehouse receiving and shipping only (scope set by Prof. Shasha)
**Method:** encode the vertical using only the current core, change no core code
**Reproduce:** `python -m operations_nerd.experiments.warehouse_ugly_encoding`
**Result:** 4 pass, 3 degraded, 5 fail across 12 steps

---

## Why this experiment exists

`schema.sql` states the falsifiability rule: needing a new table or a new
relationship, as opposed to a new field, is a true break in the core thesis,
*assuming it survives a correctly rewritten Config Pack.*

That last clause is the reason for this file. Warehouse obviously wants new
tables. The question the rule actually asks is whether a sufficiently
determined pack author could avoid them. So this is a deliberate attempt to
encode a warehouse with zero core changes, pushed until it broke, with the
breaking point recorded precisely.

Everything below was run against the unmodified core. `git diff --stat` is
empty; the only new paths are `operations_nerd/experiments/` and `CLAUDE.md`.

---

## What passed

**Step 1, partner as a contact with `role`.** A supplier is an organisation you
deal with, which is what `contacts` is for, and `role` is single-valued, which
is what the EAV table is for. Not a break in any sense.

**Step 2, carrier as a contact with `role`.** Same shape, same result.

**Step 5, order lines as follow_ups.** Worth flagging, because it contradicts
what I predicted before reading the code. I expected repeated rows to be the
break. They are not. `follow_ups` already provides a one-to-many against a
parent, and a single line's quantity and price are single values, so the EAV
table handles them cleanly.

**Step 12, the approval gate wrapping a state change.** I also predicted this
would break, and it does not. `action_type` is a free pack-defined string and
`payload` is free-form JSON, so `post_inventory_receipt` sits in
`drafted_actions` exactly as `send_follow_up_email` would: status
`pending_approval`, auto-approve gate active. The approval layer is not part of
the problem, and saying so narrows what has to be argued for.

---

## What degraded

**Step 3, item as a contact.** Works mechanically and reads back fine. But the
row carries `email=None`, `phone=None`, `status='active'` — columns that exist
because `contacts` means "someone you deal with". A bolt inherits a
relationship status it cannot have. This is the first record in the system that
is not a person or an organisation. Recorded as a modelling smell rather than
scored either way, because calling it a clean pass or a break would both be
dishonest.

**Step 4, order header as a contact.** Stores, but `supplier_id` is a bare
integer inside a JSON-encoded TEXT value. Overwriting it with `999999`
succeeded silently. Not a foreign key, not enforced, nothing cascades.

**Step 7, all lines stuffed into one JSON attribute.** The obvious workaround,
and it half-works in an instructive way. `set_entity_attributes` json-encodes
values, so the list does round-trip intact. But `find_entity_ids_by_attribute`
runs `WHERE value = ?` against the whole encoded string: matching the full blob
returns 1 hit, querying for `BOLT-M6` inside it returns 0. So "which orders
contain BOLT-M6" is unanswerable. Stored but not queryable, not joinable, and
not individually updatable.

---

## Where it actually breaks

### The break is the second reference, not the repeated rows

**Step 6, order line must point at both its order and its item.**
`follow_ups.contact_id` is the single FK and the order already owns it, so the
item reference has to live in an attribute. Overwriting `item_id` with `999999`
succeeded with no error. No integrity, no cascade, no ON DELETE.

**Step 8, shipment must point at an order, a partner and a carrier at once.**
The cleanest demonstration. `contacts` has one FK (`business_id`),
`follow_ups` has one (`contact_id`). Both `partner_id` and `carrier_id` were
overwritten with `999999` without complaint. Three simultaneous references
cannot be expressed in a schema offering one.

**Step 9, partial receipt.** Two receipts against one order, 60 then 440. The
arithmetic works — total received sums to 500 — and that is the only thing that
does. Receipt two's `item_id` was silently corrupted to a nonexistent id.
Compounds step 6 rather than introducing a new failure.

### And it breaks upstream of the database too

**Step 10, a pack cannot describe an event containing line items.**
`EventSchema.extract_fields` is typed `list[str]`. A nested entry is rejected by
pydantic before the database is ever touched: `Input should be a valid string
(loc=extract_fields.1)`. The pipeline cannot parse a purchase order email at
all, independent of how the result would be stored.

**Step 11, a pack cannot declare a record type, and fails silently doing it.**
This is worse than a rejection. `Pack(...)` does **not** raise. Pydantic's
default `extra='ignore'` means a `record_types` block is silently dropped:
`hasattr(pack, 'record_types')` returns False, with no `PackLoadError` and no
warning. A pack author could write record types into their YAML, load the pack
successfully, see no complaint, and have the entire block vanish. The `Pack`
model declares exactly `id, version, display_name, questions, event_schemas,
prompts, approval_defaults` — vocabulary can be extended, nouns cannot.

---

## Classification

| | |
|---|---|
| Pack-authoring gap | No. The pack format has no field a better author could have used, and the one they might try is silently discarded. |
| Implementation bug | No. Every failure is the schema and the loader behaving as documented and intended. |
| **True break** | **Yes, on two counts: a new relationship (a second reference per record) and a new record type (no pack-declarable nouns).** |

This meets the rule in `schema.sql` on its own terms, and it survives the
rewritten-pack clause, because the attempt is the experiment above.

**One honest qualification.** This tests warehouse receiving and shipping only,
one vertical, scoped by the professor. It shows the current core cannot express
goods. It does not by itself show that goods in general need a core change —
that claim needs a second goods vertical to hold up.

---

## Proposed next step (Harsh's proposal, not mine)

Do not add `items`, `carriers`, `orders` and `shipments` as tables. That solves
the warehouse and leaves the next goods vertical in the same position.

Add one generic mechanism instead: a core `records` table plus a `links` table,
with record types declared from the pack. The EAV table was already built
anticipating new entity types, so this extends an existing intent rather than
cutting across it.

Specced in `SPEC_RECORDS_AND_LINKS.md`, prototyped and verified in
`prototype/prototype_records_links.py`.

What it preserves: config still covers fields and vocabulary, and no
vertical-specific code enters the core.

What it honestly costs: the thesis moves from "new industries need no code" to
the weaker, more defensible "config covers fields, vocabulary and
relationships; when a structurally new shape appears, the core grows one
generic mechanism, once." Whether that counts as the thesis holding or breaking
is a call for Amaan and Prof. Shasha, not for this document.

**The claim that would then need testing:** after that single extension, a
second goods vertical is pure config. If it needs another core change, the
weaker thesis fails too.

---

## Open questions

1. Does "one generic extension, then goods verticals are config" count as the
   thesis holding or breaking? (For Prof. Shasha.)
2. `records` + `links` replacing `contacts` and `follow_ups`, or sitting beside
   them? Beside is safer for Amaan's four packs; replacing is cleaner.
3. `extract_fields` needs to express a repeating group regardless of which
   storage option wins. Worth fixing separately, since it blocks the pipeline
   independently.
4. Should `Pack` be `extra='forbid'`? Unrelated to the thesis, but step 11
   shows unknown keys vanish without warning today, which will bite a pack
   author eventually.
5. Which second goods vertical tests the extension? Without one, this is a
   single data point.
