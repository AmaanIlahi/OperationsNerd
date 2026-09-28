# Branch notes: warehouse / goods extension

**Branch:** `jasmeet/warehouse-encoding-experiment`
**Owner:** Jasmeet · **Reviewer:** Harsh · **Decisions with:** Amaan, Prof. Shasha

One page, kept current. Read this first to pick up the thread.

---

## Where this stands in one paragraph

Prof. Shasha asked for the goods side, scoped to a warehouse doing receiving and
shipping. I tried to build it with the current core and no code changes, which
is the "correctly rewritten Config Pack" attempt that `schema.sql` requires
before anything can be called a break. It fails, on a new relationship and a new
record type. The proposed fix is one generic mechanism (records plus links
declared from the pack), prototyped and tested. Nothing in the core has been
modified. Everything below is either done, or waiting on a decision that is not
mine to make.

---

## What is on the branch

| Path                                                       | What it is                                                  |
| ---------------------------------------------------------- | ----------------------------------------------------------- |
| `operations_nerd/experiments/warehouse_ugly_encoding.py`   | The encoding attempt. 12 steps against the unmodified core. |
| `operations_nerd/experiments/WAREHOUSE_FAILURE_NOTES.md`   | The writeup and the classification.                         |
| `operations_nerd/experiments/warehouse_failure_notes.json` | Machine-readable results.                                   |
| `operations_nerd/tests/test_pack_compat_smoke.py`          | Compatibility baseline for the existing packs.              |
| `operations_nerd/tests/test_approval_defaults_gap.py`      | The approval-defaults gap: current vs intended.             |
| `prototype/prototype_records_links.py`                     | The proposed extension, standalone.                         |
| `prototype/test_records_links.py`                          | Focused tests for it, including declaration validation.     |
| `prototype/SPEC_RECORDS_AND_LINKS.md`                      | The spec.                                                   |
| `CLAUDE.md`                                                | Guardrails the work was done under.                         |

### Running everything

```bash
python -m operations_nerd.experiments.warehouse_ugly_encoding   # 4 pass, 3 degraded, 5 fail
python operations_nerd/tests/test_pack_compat_smoke.py          # 36 checks
python operations_nerd/tests/test_approval_defaults_gap.py      # 20 checks
python prototype/prototype_records_links.py                     # all checks behave
python prototype/test_records_links.py                          # 36 checks
```

Plain scripts, matching the existing convention in `operations_nerd/tests`. No
pytest, no new dependencies. All exit non-zero on failure.

---

## The finding

**Passed, contradicting my own predictions in two places.** Partner and carrier
are clean as role contacts. Order lines survive as follow_ups, because
`follow_ups` already gives a one-to-many and a line's quantity and price are
single values — repeated rows are _not_ the break. The approval gate handles a
state change exactly like a message, so it is not part of the problem either.

**Broke on the second reference.** `follow_ups` has one FK and the order owns
it, so an order line's item reference degrades to an untyped integer:
`item_id=999999` against a nonexistent record was accepted with no error. A
shipment needs three simultaneous references and there is no encoding for that.

**Broke upstream of the database too.** `EventSchema.extract_fields` is typed
`list[str]`, so a pack cannot describe a PO email containing line items. And a
`record_types` block is silently dropped by pydantic's default `extra='ignore'`
— no error, no warning, the block vanishes.

**Classification: true break on two counts**, a new relationship and a new
record type. Not a pack-authoring gap, not an implementation bug. Harsh has
reviewed and agreed.

**Qualification, deliberately kept in the writeup:** this is one vertical. It
shows the current core cannot express goods. It does not show that goods in
general need a core change. That needs a second goods vertical.

---

## The proposal

Four tables beside `contacts` and `follow_ups`, nothing existing modified.
`record_types` and `link_types` hold what the pack declares; `records` and
`record_links` hold the data. Fields reuse `entity_attributes` unchanged with
`entity_type='record'`.

Bad references are rejected structurally (two real FKs with a CHECK, rather than
one polymorphic untyped column) and semantically (one trigger, three ordered
checks). Each failure reports its own cause.

Declaration validation lives in the **prototype** loader, per Harsh's
instruction, not in `packs/loader.py`.

### What it does to the thesis

Original: new industries need new packs, not new code. False as stated, for
goods.

Proposed replacement: config covers fields, vocabulary and relationships; the
core grows one generic mechanism when a structurally new shape appears, and that
mechanism is not vertical-specific. **Warehouse cost one extension. The claim is
that the next goods vertical costs zero — and that is what can now fail.**

---

## Open decisions

Nothing here is mine to decide. Each needs a named person.

**1. Does the weaker thesis count as holding or breaking?** → Prof. Shasha.
If goods cost one generic extension and subsequent goods verticals are pure
config, is that the thesis surviving or the thesis being rewritten? The answer
changes how the result is written up, so it is worth settling before the writeup
rather than after.

**2. Should `packs/loader.py` validate `record_types`?** → Amaan, then Harsh.
Currently an unsupported block vanishes silently. The validation exists in the
prototype loader and is tested; the core change is proposed, not applied.
Related and separable: should `Pack` be `extra='forbid'` generally, so that _any_
unknown key errors rather than disappearing? That would have caught this class
of bug on its own.

**3. Should `create_business` seed approval policies from the pack?** → Amaan.
Found while writing the compatibility test and confirmed by Harsh. The loader
validates `approval_defaults` but nothing writes them to `approval_policies`, so
both packs declare an escalate action as auto-approve and neither takes effect.
It fails safe (ask-first), so nothing unsafe happens, but a documented pack
setting has no runtime effect.
`test_approval_defaults_gap.py` Part A pins the current behaviour; Part B
demonstrates the fix without touching the core. If the decision is taken, Part B
is the spec and Part A should be deleted.

**4. `records` + `links` beside `contacts` and `follow_ups`, or replacing
them?** → Amaan, then Prof. Shasha.
Beside is safer and is what is built. Replacing is cleaner and would make
contacts just another record type. Not urgent, but it decides whether this is a
bolt-on or the future shape of the core.

**5. Which second goods vertical?** → Prof. Shasha.
Without one, the "next vertical costs zero" claim is untested and the result is
a single data point.

---

## Known gaps, not yet worked

- **`extract_fields` is a separate end-to-end blocker.** The storage extension
  solves typed records and links, but a pack still cannot ingest nested line
  items until the event schema can describe them. Agreed with Harsh to keep it
  separate so each change stays reviewable, but the dependency is real: the
  warehouse is not end-to-end until both land.
- **Two packs on disk, not four.** `realestate` and `healthclub`. The
  compatibility test discovers packs from the filesystem, so restaurant and
  clinic are picked up automatically when they land.
- **No inventory or thresholds.** Out of scope per Prof. Shasha's scoping to
  receiving and shipping. Pre-planning will likely force the question.
- **No pipeline integration and no UI.** Deliberate.

---

## Next

1. Live demo of the prototype. Five beats: the pack YAML, an order with a line
   pointing at both order and item, a shipment with three references, a bad
   reference rejected on screen, the "which orders contain this item" query.
2. Take the proposal to Amaan and Prof. Shasha once Harsh has reviewed.
3. Pick a second goods vertical, which is the only thing that tests the actual
   claim.
