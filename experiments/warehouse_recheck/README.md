# Warehouse re-check

This folder checks the warehouse findings marked "(a) fixed" against current `main` (at `dbff470`, branch `jasmeet/warehouse-recheck`).

`test_recheck.py` is a standalone script. It imports `operations_nerd/` to test it and does not change it. The script:

- builds a small warehouse business through the real API with FastAPI's `TestClient`. The business has Item, Warehouse, Order, Order line and Stock level, plus their links.
- creates the schema with a stubbed chat turn (the `agentic.chat.run_llm` pattern from `tests/test_agentic_phase_d.py`), so no model is called.
- points `DATABASE_PATH` at a new temporary file before `db` is imported, and stops if that didn't take effect. The real database is never created or wiped.

```
python experiments/warehouse_recheck/test_recheck.py      # from the repo root, with ./venv
```

It prints one line per check (finding, expected, actual, PASS/FAIL) and exits with a non-zero code if any check fails.

## Results (2026-10-22)

**12/12 checks passed.**

| Finding | What is checked | Expected | Actual | Result |
|---|---|---|---|---|
| 3 | An Item entity created with SKU and Unit | Fields are exactly SKU and Unit. No email, phone or status field | `['SKU', 'Unit']`, types `text`, `select` | PASS |
| 4 | PATCH an existing link to 999999 | 422 naming the link, and a GET still shows the old target | 422 naming the link. GET shows the old target | PASS |
| 6 | Stock level with 2 valid links. PATCH Location to 999999 | 422. Both links unchanged | 422. Both unchanged | PASS |
| 8 | Order line with 3 links. PATCH each in turn to 999999 | 422 every time. The other two unchanged | 422 ×3. The others are unchanged each time | PASS ×3 |
| 7 | Two orders whose lines link to different items: "which orders contain item X?" | The core helpers return the right order for each item | X-1 → its order, Y-1 → its order | PASS ×2 |
| 7 (API) | Is there an API endpoint that answers it? | None (the gap is still open) | 4 record routes (create, list, get, patch). `list ?<item link>=X` ignores the filter and returns all 3 lines | PASS (confirms the gap) |
| 11a | `validate_spec` with an unknown top-level key | Rejected, naming the key | `surprise: Extra inputs are not permitted` | PASS |
| S3 | POST a record with an undeclared link key `l_undeclared` | 422 naming the key | 422 `{"l_undeclared": "unknown or archived field or link"}` | PASS |
| S4 | POST to an undeclared entity type | 404 | 404 `Entity type not found` | PASS |

After this run, the full non-live suite (every `operations_nerd/tests/test_*.py` except `test_llm_smoke.py`, each with its own temporary `DATABASE_PATH`) still passes: 18 of 18 files.

### Limits of what these checks show

- **3** shows that the operations engine adds nothing beyond what it is asked for. When the turn asks for SKU and Unit, Item gets exactly SKU and Unit. Because the model is stubbed, it can't show which fields a live model would propose for an Item.
- **7** answers the question in two steps with core helpers: `db.find_entity_ids_by_attribute` finds the Order line records whose Item link is X, then `db.get_entity` reads each line's Order link. That helper filters by entity type only, not by business. It is safe today only because entity keys are random per business. The script checks `business_id` itself as well.

## Findings still open

- **7: no API to query records by link.** The core helpers can answer "which orders contain item X", but no endpoint exposes it. `GET /records/{entity}` takes only `limit` and `offset`, and there is no reverse-link view.
- **9 (partly): the stock module's `item_id` isn't tied to a core record.** Nothing checks that it names a real Item. This is resolved when the module interface lands.
- **S1: links have no "required" flag.** A record can always be saved without a link value, even when the business needs one, such as an order line with no item.
- **S2: no cascade on delete.** The `record_links` prototype only restricts deletes (its foreign keys have no `ON DELETE`), and `main` has no record delete path. What happens to linked records on delete is undecided.
- **S3 and S4 at the database level:** the `record_links` prototype accepts undeclared link keys and entity types. Only app code (`agentic/records.py`) catches them, and the checks above test that layer, not the database.

Legacy pack layer only. Production already disables it (`APP_ENV=production` drops the legacy routes in `main.py`):

- **10: `EventSchema.extract_fields` is `list[str]`.** Extraction fields are flat strings, so line items (a list of item and quantity pairs) can't be described.
- **11b: `Pack` silently drops unknown keys.** The model has no `extra="forbid"`, unlike the agentic `Spec`, so a typo in a pack file is ignored instead of rejected.
- **B1: pack `approval_defaults` never reach `approval_policies`.** Nothing writes the defaults to the table. It fails safe: every action falls back to needing approval.

### Hardening note

- **`find_entity_ids_by_attribute` is not scoped by business.** It filters by entity type only, which is safe today only because entity keys are random per business. Any endpoint built on it (for example for 7) should add a `business_id` filter. The planned `record_links` table with `business_id` would cover this.
