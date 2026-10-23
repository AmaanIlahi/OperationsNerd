# Food distributor: results

Run on 2026-10-23 against `jasmeet/food-distributor` at fc4940f (core as on main), with Python 3.11.9 and SQLite 3.45.1. The stock engine is `jasmeet/logistics-stock` at b1b74ce. The predictions are in [PREDICTIONS.md](PREDICTIONS.md) and were not edited.

Script: [test_food_config.py](test_food_config.py). Run it with:

```
./venv/Scripts/python.exe -B experiments/food_distributor/test_food_config.py
```

How it was run (the Method section of PREDICTIONS.md):
- **Two databases.** A (produce: kg, short shelf life) and B (frozen: cases, long shelf life) each run in their own process, on their own fresh temporary database. They need separate processes because `db.DB_PATH` is fixed when the module is imported.
- **Chat path only.** Every data shape went through chat, then proposal, then approval. The model call was replaced by scripted operation lists, as the existing tests do. No spec was edited directly, and no model was called.
- **Records** went through the records API.
- **The stock engine** was read with `git show` into a temporary folder and run unchanged, on each company's own database. No module code was built.
- **Dates** are fixed to 2026-10-23, so the expiry window gives the same answer on any day.

## Every row

| # | Requirement | Predicted | Observed | Right? |
|---|---|---|---|---|
| D1 | Product fields | pass | pass. SKU, unit, storage temperature and shelf life days were built by chat and read back unchanged in A and B | right |
| D2 | Suppliers and customers | pass | pass | right |
| D3 | Purchase orders with lines | pass | pass. The line entity has many_to_one links to the order and to the product. linked-by on the order lists exactly its lines | right |
| D4 | Sales orders with lines | pass | pass, same shape as D3 | right |
| D5 | Lots with dates and links | pass | pass. Received and expiry dates are stored, and the links to product, supplier and purchase line all resolve | right |
| D6 | Lot number unique per product | config gap | config gap. Chat refused `unique` on update_field and on add_field ("not an accepted attribute"). A second lot with the same number for the same product was saved (201) | right |
| D7 | Order line must have a product | config gap | config gap. Chat refused `required` on add_link, and update_field can't target a link ("Field 'l_…' does not exist"). A sales line with no product was saved (201) | right |
| D8a | Exact money and kg | config gap | config gap. Type `decimal` was refused. Stored values read back exactly (1.1, 2.2, 4.35), but adding them gives A 1.1 + 2.2 = 3.3000000000000003 and B 3 × 4.35 = 13.049999999999999 | right |
| D8b | Decimal comma "1,5" kg | observed during review, not predicted | silently wrong. Saved with 201 and stored as 15 | n/a (not a prediction) |
| D9 | Lot quantity as a config field | silently wrong | silently wrong. Chat added Lot.Quantity, which was set to 100. The engine then received 100, reserved 30 and shipped 30, so the field still said 100 while the ledger said 70. Editing the field to 250 was accepted and moved nothing in the ledger | right |
| D10 | Sales line records which lots it shipped from | module gap | module gap. A config link from sales line to lot holds one lot: setting L2 replaced L1, and a list of lots was refused (422), so a split line loses a lot. The engine has no lot | right |
| D11 | One PO line received as several lots | pass for the shape; module gap for the stock | pass for the shape. Two lots link to one purchase line and linked-by lists both. Module gap for the stock: `receive` has no lot, movements have no lot column, and `source_ref` took "any text at all, not a PO line" | right |
| R1 | Line and order totals | config gap | config gap. Type `formula` and a `formula` attribute were both refused. Saved lines and orders hold no total | right |
| R2 | Expiry after received | config gap | config gap. `add_rule` is not an operation. A lot received 2026-10-23 that expires 2026-10-01 was saved | right |
| R3 | Order status flow | pass for the field; module gap for the flow | pass for the field: a select holds the six statuses. Nothing enforces the flow: delivered → draft → cancelled → shipped all saved. The core has no flow operation, and the engine has no order status | right |
| Q1 | Lots expiring in 7 days | config gap | config gap. The list endpoint takes only `limit` and `offset`. With filter parameters it returned all 6 lots (3 were due in A, 2 in B). `find_entity_ids_by_attribute` is exact match only | right |
| Q2 | Recall: who received lot X | module gap, plus D10 | module gap, plus D10. The truth for L2 is both customers. Through the config line-to-lot link, the answer misses the customer whose line was split. The engine has no lot | right |
| P1 | Field-role mapping | config gap | config gap. Product.Unit is `f_3d578136` in A and `f_a325842e` in B. One template field got a different key in each business. After renaming Unit to "Selling unit", no field is labelled Unit. `role` was refused, and a field has only key, label, type, options, required, archived | right |
| M1 | Stock per lot | module gap | module gap. Movements and reservations have no lot column | right |
| M2 | Oldest expiry reserved first | module gap | module gap. `reserve(business_id, item_id, quantity, order_ref, idempotency_key)`: the engine code has no lot, no expiry and no ordering | right |
| M3a | Expired lots blocked | module gap | module gap. The engine has no expiry. An expired lot, used as the item, was reserved (5 of 5) in A and B | right |
| M3b | Held lots blocked | module gap, needs P1 | module gap, needs P1. Config can set an "On hold" boolean on a lot, but the engine code has no hold, and it can't find the config field without P1 | right |
| M4 | kg (A) and cases (B) | module gap, needs P1 and items from the core | module gap, needs P1 and items from the core. Receiving 1.5 was rejected ("quantity must be a positive integer"). 40 cases were accepted, so B fits today. Item 987654, which exists nowhere, was accepted. The engine has no unit or scale | right |
| M5 | Customer minimum shelf life | config gap (P1) plus module gap | config gap (P1) plus module gap. A stored "at least 5 days left" and B stored "at least 70 percent of shelf life left" on Customer by config. The engine has no customer or expiry | right |
| M6 | Catch weight | candidate true break | module gap. B's line held 10 cases and 183.4 weighed kg as two config fields. The engine reserved and shipped 10 cases for B. `ship` has no measured quantity, so recording the weight at shipping needs module code. The kg × price amount is R1. No step needed code for one company | **wrong** |
| T1 | A and B on one module | pass, if P1 exists and units, shelf-life policy and FEFO are data, not code | not testable: the precondition isn't met (P1 is a config gap, and M1 to M4 aren't built). Today one engine serves both: B's cases fit, but A's 1.5 kg is rejected | untested |

## Summary

There are 25 predictions, and 24 could be tested: 23 were right and 1 was wrong (M6). T1 couldn't be tested. D8b isn't counted because it wasn't a prediction.

Observed classes. A row with two parts (D11, R3, M5) counts once in each class it names.

| Class | Rows | Count |
|---|---|---|
| pass | D1, D2, D3, D4, D5, D11 (shape), R3 (field) | 7 |
| config gap | D6, D7, D8a, R1, R2, Q1, P1, M5 (P1) | 8 |
| module gap | D10, D11 (stock), R3 (flow), Q2, M1, M2, M3a, M3b, M4, M5 (module), M6 | 11 |
| silently wrong | D9, D8b (D8b was observed during review, not predicted) | 2 |
| true break | none | 0 |
| not testable | T1 | 1 |

The predictions had 10 module gaps and 1 candidate true break. The difference is M6.

## Wrong predictions

**M6, catch weight.** Predicted: candidate true break. Observed: module gap.
- The prediction's reason was that one line carries two units, and that M4's single unit and scale per item can't hold the weighed amount.
- In the run, the stock unit stayed cases. The engine already holds cases for B and shipped 10 of them. The weighed kg is data on the line, not a stock unit, and config held it as a second number field next to the case quantity.
- Two things are missing: `ship` can't record a measured weight (module), and the kg × price amount isn't computed (R1). Neither needs code for one company.
- The prediction said this "becomes a module gap if the module supports a second, measured quantity per line for every business". That is what was observed. The row is still marked wrong because the class predicted was true break.
- Limit: the module isn't built, so the experiment doesn't show that weight capture can be written once for every business. It shows only that nothing in the shape or the stock unit needed per-company code.

## Also seen, not in the predictions

- **Allocation rows can be built by config, but nothing checks them (D10, Q2).**
  - Chat can build a "Lot allocation" entity (sales line, lot, quantity). Hand-entered rows answered the L2 recall correctly, with both customers.
  - The same entity accepted 500 from a sales line of 5, against a lot whose stock nothing tracks. That is the D9 problem again.
  - So the shape of the allocation rows is expressible today. What's missing is the module writing them from real shipments.
- **A lot id can stand in for the item (M1, M3a).** `item_id` is unchecked, so passing a lot id tracks stock per lot today. But nothing sums lots per product, orders them by expiry or knows that a lot has expired: the expired lot was reserved.
- **Q1's claim about range filters holds.** A SQL range filter on the stored JSON text worked for dates in A and B, but not for numbers:
  - A: "shelf life > 8" returned nothing and missed the 10-day product.
  - B: "shelf life > 100" also returned the 90-day product.
- **M5's two forms are both storable.** A's rule is in days. B's is a number plus a days/percent select. Whether one module can serve both forms without per-company code can't be tested until the module exists. The prediction makes this the line between a module gap and a true break.
- **What this experiment measures.**
  - It measures two things: whether chat can express a requirement, and whether the engine has it today.
  - Where a missing rule belongs, in a core change (config gap) or in module code (module gap), follows the class definitions. It isn't measured. This matters most for R3's status flow, which could go either way.
