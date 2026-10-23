# Warehouse re-check

This folder checks the warehouse findings marked "(a) fixed" against current `main` (merged into `jasmeet/warehouse-recheck` at `ef54184`, which includes PRs #16 to #19).

`test_recheck.py` is a standalone script. It imports `operations_nerd/` to test it and does not change it. The script:

- builds a small warehouse business through the real API with FastAPI's `TestClient`. The business has Item, Warehouse, Order, Order line and Stock level, plus their links.
- creates the schema with a stubbed chat turn (the `agentic.chat.run_llm` pattern from `tests/test_agentic_phase_d.py`), so no model is called.
- points `DATABASE_PATH` at a new temporary file before `db` is imported, and stops if that didn't take effect. The real database is never created or wiped.
- for 11b, copies the shipped `realestate` pack into the same temporary folder and edits only the copy.

```
python experiments/warehouse_recheck/test_recheck.py      # from the repo root, with ./venv
```

It prints one line per check (finding, expected, actual, PASS/FAIL) and exits with a non-zero code if any check fails.

The non-live suite runs with the one-command runner from PR #16. That runner does not set `DATABASE_PATH`, and most test scripts call `init_db(reset=True)` on whatever path it resolves to. So set a temporary path yourself first:

```
DATABASE_PATH="$(mktemp -d)/suite.db" LLM_PROVIDER=stub ./venv/Scripts/python.exe scripts/run_tests.py   # Git Bash
```

## Results (2026-10-23)

**16/16 checks passed.**

| Finding | What is checked | Expected | Actual | Result |
|---|---|---|---|---|
| 3 | An Item entity created with SKU and Unit | Fields are exactly SKU and Unit. No email, phone or status field | `['SKU', 'Unit']`, types `text`, `select` | PASS |
| 4 | PATCH an existing link to 999999 | 422 naming the link, and a GET still shows the old target | 422 naming the link. GET shows the old target | PASS |
| 6 | Stock level with 2 valid links. PATCH Location to 999999 | 422. Both links unchanged | 422. Both unchanged | PASS |
| 8 | Order line with 3 links. PATCH each in turn to 999999 | 422 every time. The other two unchanged | 422 ×3. The others are unchanged each time | PASS ×3 |
| 7 (core) | Two orders whose lines link to different items: "which orders contain item X?" | The core helpers return the right order for each item | X-1 → its order, Y-1 → its order | PASS ×2 |
| 7 (API) | The same question through the API: `GET /records/{item}/{X}/linked-by`, then GET each linking order line | The right order for each item | X-1 → its order, Y-1 → its order, 2 calls each (1 line per item) | PASS ×2 |
| 7 (one call) | Can a single call answer it? | No (the gap is only partly closed) | `linked-by` returns the lines as `id` and `label` only, not their Order link. `list ?<item link>=X` still ignores the filter and returns all 3 lines | PASS (confirms the remaining gap) |
| 11a | `validate_spec` with an unknown top-level key | Rejected, naming the key | `surprise: Extra inputs are not permitted` | PASS |
| 11b (top level) | Copy of the shipped `realestate` pack, unchanged, then with `record_types:` added to `pack.yaml` | Unchanged copy loads. With the key: rejected, naming it | Loads. `pack.yaml: unknown top-level key 'record_types'` | PASS |
| 11b (nested) | Same copy with `line_items` added inside an event schema | Rejected, naming the key | `event_schemas.0.line_items: Extra inputs are not permitted` | PASS |
| S3 | POST a record with an undeclared link key `l_undeclared` | 422 naming the key | 422 `{"l_undeclared": "unknown or archived field or link"}` | PASS |
| S4 | POST to an undeclared entity type | 404 | 404 `Entity type not found` | PASS |

After this run, the full non-live suite (`scripts/run_tests.py`, which skips `test_llm_smoke.py`, run with a temporary `DATABASE_PATH`) still passes: 21 of 21 scripts.

### Limits of what these checks show

- **3** shows that the operations engine adds nothing beyond what it is asked for. When the turn asks for SKU and Unit, Item gets exactly SKU and Unit. Because the model is stubbed, it can't show which fields a live model would propose for an Item.
- **7 (core)** uses `db.find_entity_ids_by_attribute` to find the Order line records whose Item link is X, then `db.get_entity` to read each line's Order link. That helper filters by entity type only, not by business, so the script checks `business_id` itself.
- **7 (API)** uses `linked-by`, which is scoped by business (it reads through `db.list_entities(conn, business_id, ...)`, and another account's business returns 404). It covers only links with an editable cardinality (`many_to_one`, `one_to_one`), which is the case for Order line → Item.
- **11b** was also run against the pre-fix loader (`operations_nerd/packs/loader.py` at `3c1097c`, loaded from a scratch copy). Both edited packs loaded without error there, so these checks do tell the old and new behaviour apart.

## Fixed since the last run

- **11b: `Pack` silently dropped unknown keys.** Fixed by PR #18. `Pack`, `Question`, `EventSchema` and `EntitySchema` now use `extra="forbid"`, and `load_pack` rejects unknown top-level keys in every pack file, naming each one.

## Findings still open

- **7 (partly): no single API call answers "which orders contain item X".** `GET /records/{entity}/{id}/linked-by` (PR #19) handles the reverse hop: it lists the order lines that link to X. But it returns each line as `id` and `label` only, so getting each line's Order takes one more GET per line (1 + N calls). `GET /records/{entity}` still takes only `limit` and `offset` and ignores a link filter.
- **9 (partly): the stock module's `item_id` isn't tied to a core record.** Nothing checks that it names a real Item. This is resolved when the module interface lands.
- **S1: links have no "required" flag.** A record can always be saved without a link value, even when the business needs one, such as an order line with no item.
- **S2: no cascade on delete.** The `record_links` prototype only restricts deletes (its foreign keys have no `ON DELETE`), and `main` has no record delete path. What happens to linked records on delete is undecided. `linked-by` now gives a future delete flow the "what points at this record?" answer it would need.
- **S3 and S4 at the database level:** the `record_links` prototype accepts undeclared link keys and entity types. Only app code (`agentic/records.py`) catches them, and the checks above test that layer, not the database.

Legacy pack layer only. Production already disables it (`APP_ENV=production` drops the legacy routes in `main.py`):

- **10: `EventSchema.extract_fields` is `list[str]`.** Extraction fields are flat strings, so line items (a list of item and quantity pairs) can't be described. Since 11b was fixed, an attempt to add them (as the `line_items` check does) is now rejected at load time instead of silently dropped.
- **B1: pack `approval_defaults` never reach `approval_policies`.** Nothing writes the defaults to the table. It fails safe: every action falls back to needing approval.

### Hardening notes

- **`find_entity_ids_by_attribute` is not scoped by business.** It filters by entity type only, which is safe today only because entity keys are random per business. `linked-by` doesn't use it, but any future link-filter endpoint built on it should add a `business_id` filter. The planned `record_links` table with `business_id` would cover this.
- **`linked-by` scans every record of each source entity.** For each inbound link it loads all of the business's records of that entity type and compares the link value in Python. That's fine at prototype sizes. A `record_links` table with an index on the target would turn it into a lookup.
