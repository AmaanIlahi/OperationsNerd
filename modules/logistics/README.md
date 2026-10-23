# LogisticsNerd stock engine (v3: opening stock and shipment records)

LogisticsNerd's stock engine: receive stock, reserve it for orders, ship it and release what isn't needed.
- v2 adds optional lots with expiry dates, holds and recall.
- v3 adds opening stock, receive-and-reserve in one transaction, and shipments as records: lines, a single dispatch and tracking events.

It is standalone Python 3.11 with stdlib `sqlite3` only. It imports nothing from `operations_nerd/` and makes no LLM calls.

Files:
- [stock.py](stock.py): the commands and queries
- [schema.sql](schema.sql): the tables, constraints and triggers
- [tests/test_stock.py](tests/test_stock.py): the v1 tests, unchanged since v1
- [tests/test_lots.py](tests/test_lots.py): the lot tests, unchanged since v2
- [tests/test_shipments.py](tests/test_shipments.py): the v3 tests
- [tests/mutation_check.py](tests/mutation_check.py): checks that the tests catch the removal of each protection

## Design rules

- Stock is a ledger of movements plus reservations. No caller ever edits a quantity in place:
  - **on hand** = sum of `stock_movements.quantity`
  - **reserved** = sum of `quantity - quantity_shipped` over active reservations
  - **available** = on hand − reserved

  `quantity_shipped` is kept by the database, equal to the sum of that reservation's shipments. See [Why quantity_shipped is stored](#why-quantity_shipped-is-stored).
- Every row carries `business_id`. Every command takes an idempotency key, unique per business.
- Items are referenced by `item_id` only. There is no item catalog; items will come from the core later.
- `location_id` is on every stock row, but v1 always writes location 1.
- Lots, holds and allocations are module tables, not config fields. The food distributor experiment (`experiments/food_distributor/RESULTS.md` on the `jasmeet/food-distributor` branch, rows D9, D10 and Q2) found that a quantity kept in config drifts from the ledger. So anything that counts stock belongs to the module.
- The module can't read config yet, so everything it needs comes in as command inputs: whether an item is lot-tracked (`track_lots`), the lot number and expiry date (`receive`), the business date (`as_of`) and hold reasons. Later these inputs will come from config through the module interface, for example an item's "lot tracked" field calling `track_lots`. The commands stay the same.

## Commands

Each command runs in one `BEGIN IMMEDIATE` transaction. `conn` comes from `stock.connect(path)`, which sets:
- autocommit mode, which the explicit transaction relies on
- `PRAGMA foreign_keys = ON`
- WAL and a 10 s busy timeout

| Command | What it does | Returns |
|---|---|---|
| `receive(conn, business_id, item_id, quantity, source_ref, idempotency_key, lot_number=None, expiry_date=None)` | Adds stock: one positive `receipt` movement. `source_ref` is the purchase order line being received. A lot-tracked item needs `lot_number`; any other item takes none. | `movement_id`, `item_id`, `received`; for a lot, also `lot_id`, `lot_number`, `expiry_date` |
| `reserve(conn, business_id, item_id, quantity, order_ref, idempotency_key, as_of=None)` | Reserves up to what's available. If nothing is available, it writes no reservation and `reservation_id` is `None`. A lot-tracked item needs `as_of` and gets allocations, see [Lots and expiry](#lots-and-expiry). | `reservation_id`, `item_id`, `requested`, `reserved`, `shortage`; for a lot-tracked item, also `allocations` |
| `release(conn, business_id, reservation_id, idempotency_key)` | Frees the unshipped remainder of an active reservation and marks it `released`. What already shipped stays shipped. For a lot-tracked item this frees what is left of each allocation. | `reservation_id`, `released`, `status` |
| `ship(conn, business_id, reservation_id, quantity, idempotency_key, as_of=None)` | Ships part or all of an active reservation. It writes one negative `shipment` movement with `reservation_id` set. In the same statement, a database trigger raises the reservation's `quantity_shipped` and marks it `fulfilled` once everything has shipped. A lot-tracked item needs `as_of` and ships lot by lot: one movement per lot. | `reservation_id`, `shipped`, `quantity_shipped`, `remaining`, `status`, and `movement_id`; for a lot-tracked item, `lots` instead of `movement_id` |
| `open_stock(conn, business_id, item_id, quantity, source_ref, idempotency_key, lot_number=None, expiry_date=None)` | Records stock that was there before the system: an `opening` movement, not a supplier receipt. Only before any receipt or shipment of the item. Lots work as in `receive`. | Same as `receive` |
| `receive_and_reserve(conn, business_id, item_id, quantity, source_ref, order_ref, idempotency_key, lot_number=None, expiry_date=None, as_of=None, reserve_quantity=None)` | Receives stock and reserves it for a waiting order line, in one transaction. `source_ref` is the purchase reference. `reserve_quantity` (default: all of it) is how much goes to the line. For a lot-tracked item it needs `as_of` and allocates from the lot just received. | What `receive` returns, plus `reservation_id`, `order_ref`, `reserved` |
| `create_shipment(conn, business_id, shipment_number, idempotency_key)` | Starts a draft shipment. The number is unique per business. | `shipment_id`, `shipment_number`, `status` |
| `add_shipment_line(conn, business_id, shipment_id, reservation_id, quantity, idempotency_key)` | Puts part or all of an active reservation on a draft shipment. Nothing moves yet. | `line_id`, `shipment_id`, `reservation_id`, `quantity` |
| `dispatch(conn, business_id, shipment_id, carrier, idempotency_key, as_of, label_ref=None, tracking_ref=None)` | Dispatches a draft shipment, once. Needs a carrier and a label or tracking reference. It writes every line's shipment movements and the dispatch record in one transaction. | The shipment, as `shipment()` returns it |
| `record_tracking_event(conn, business_id, shipment_id, event, occurred_at, idempotency_key, detail=None)` | Appends a carrier event to a dispatched shipment. Never changes stock. | `event_id`, `shipment_id`, `event`, `status` |
| `track_lots(conn, business_id, item_id, idempotency_key)` | Turns on lot tracking for an item. Only before the item has any stock. It can't be turned off. On an item that is already tracked it changes nothing. | `item_id`, `lot_tracked` |
| `hold_lot(conn, business_id, lot_id, reason, idempotency_key)` | Puts a lot on hold: no new allocations, no shipping from it. | `lot_id`, `lot_number`, `on_hold`, `allocated` (what existing allocations still hold in the lot) |
| `release_hold(conn, business_id, lot_id, reason, idempotency_key)` | Takes a lot off hold. | Same as `hold_lot` |

`as_of` is ignored for items without lots, so those behave exactly as in v1.

Queries:
- `on_hand`, `reserved` and `available`, per business and item. `available` reads both sides in one statement. For a lot-tracked item it includes held and expired lots; `lots` says what can be used.
- `lots(conn, business_id, item_id, as_of=None)`: the item's lots, earliest expiry first. Each has `on_hand`, `allocated` (reserved, not yet shipped), `free` and `on_hold`. With `as_of`, each also has `expired` and `usable`.
- `shipment(conn, business_id, shipment_id)`: the shipment's number, its status (`draft`, `dispatched` or `delivered`), the carrier, label and tracking references and the dispatch date. Also each line with its `order_ref` and the movements it created (with lot, for lot-tracked items), and the tracking events in the order they were recorded.
- `recall(conn, business_id, lot_id)`: the lot's details, `received` (opening stock included) and `on_hand`, and every reservation it was allocated to. Each reservation has `reservation_id`, `order_ref`, `status`, `allocated` (from this lot) and `shipped` (from this lot). Released and fulfilled reservations are included.

Errors:
- `ValueError`: bad input, raised before anything is written. This includes a missing `as_of` for a lot-tracked item.
- `IdempotencyConflict`: a key reused with a different payload.
- `NotFound`: the reservation or lot isn't in this business.
- `InvalidState`:
  - The reservation is no longer active.
  - The lot is already on hold, or not on hold for `release_hold`.
  - `track_lots` or `open_stock` came after the item had stock or receipts.
  - The shipment isn't a draft (for a line or a dispatch), isn't dispatched (for a tracking event), or has no lines.
  - The shipment number is taken, or the reservation is already on that shipment.
- `OverShipment`: the shipment, line or dispatch is more than is left to ship.
- `LotMismatch`: a lot was given for an item without lots, none was given for a lot-tracked item, or the expiry date differs from the lot's.
- `LotUnavailable`: the shipment or dispatch needs stock from allocated lots that are held or expired on `as_of`, or `receive_and_reserve` received into a lot that is held or expired.

### Idempotency

Each successful command writes one row to `stock_commands` with its key, command name, payload and result:
- **Same key, same command and payload:** returns the stored result and writes nothing. This holds even if stock has changed since. A replayed `reserve` returns the original shortage, not a fresh one.
- **Same key, different command or payload:** raises `IdempotencyConflict`.
- **A command that fails records nothing,** so its key stays free for a corrected retry.

Keys are unique per business, so two businesses can use the same key.

## Lots and expiry

Lot tracking is opt-in per item. Items that aren't tracked take the v1 code paths and get the v1 results. Each command checks whether the item is tracked, and the new triggers run but pass. All 19 v1 tests pass unchanged.

- **Turning it on.** `track_lots` must come before the item's first receipt. Stock received without a lot would have no lot to be allocated from, so the command and the database both refuse an item that already has stock.
- **Receiving.**
  - Each receipt of a tracked item names a lot. The first receipt creates the lot with its expiry date. Later receipts of the same lot number add to it, and must give the same expiry date.
  - A lot is unique per business, item and lot number. The same lot number on another item or in another business is a different lot.
  - One purchase order line can be received as several lots: one `receive` per lot, with the same `source_ref` and a different idempotency key each time.
  - `expiry_date` is optional. A lot without one never expires.
- **Dates.** Dates are `'YYYY-MM-DD'` strings. A lot is **expired on any date after its expiry date**, so it can still be allocated and shipped on the expiry date itself. Expiry is always judged against the `as_of` date the caller passes in, never the clock. That keeps the tests deterministic, and lets a caller plan for a future ship date.
- **Reserving.**
  - `reserve` uses only usable lots: not held, not expired on `as_of`, and with free stock.
  - It allocates them earliest expiry first. Lots with the same expiry go in the order they were received, and lots without expiry come last.
  - Each allocation is a row (reservation, lot, quantity), and one reservation can span several lots. Whatever the usable lots can't cover is returned as `shortage`, as in v1.
- **Shipping.**
  - `ship` works through the reservation's allocations, earliest expiry first. It writes one movement per lot, keyed `<idempotency_key>/lot:<lot_id>`.
  - It passes over allocations whose lot is held or expired on `as_of`, and ships from the next lot instead.
  - If the usable allocations can't cover the quantity, it raises `LotUnavailable`, names the blocked lots and ships nothing.
- **Holds.**
  - `hold_lot` and `release_hold` each take a reason, and are idempotent by key.
  - Every hold and release is kept as a row, so the history can be read back. A lot is on hold if its latest row is a hold.
  - Holding a lot that is already held, or releasing one that isn't, raises `InvalidState`.
- **What a hold does to existing allocations: they stay.** A hold blocks new allocations and blocks shipping from the lot, but it doesn't take allocations away. They still count as reserved. Reasons:
  - The recall query still shows which orders were going to get the lot.
  - Nothing moves to another lot without a decision. A QA hold is often lifted the same day, and an automatic re-allocation would have taken those orders off fresher stock for nothing.
  - The reservation stays one fixed set of allocations, so "allocations add up to the reservation" stays a rule the database can enforce.

  The caller decides what to do. `hold_lot` returns `allocated`: what existing allocations still hold in the lot. If the hold is permanent, `release` those reservations and `reserve` again, and the new allocations come from other lots. Meanwhile `ship` passes over the held lot and ships the rest of each order from its other allocations.
- **Recall.** `recall(lot_id)` lists every reservation the lot was allocated to, with `order_ref`, status and the quantity allocated and shipped from that lot. A split order line shows up in the recall of each of its lots.

## Opening stock, receive-and-reserve and shipments (v3)

- **Opening stock** is its own movement kind, `opening`, so a stock count at go-live is never mistaken for a supplier receipt.
  - It counts in on hand like a receipt.
  - It must come before any receipt or shipment of the item. Several opening movements are fine, for example one per lot or per shelf.
  - `source_ref` says where the figure came from, such as a count sheet.
  - A lot-tracked item calls `track_lots` first, then opens stock into lots.
- **Receive and reserve** writes the receipt and the reservation in one transaction, under one idempotency key. The purchasing side never coordinates two calls: either both are written or neither is.
  - For a lot-tracked item the reservation is allocated from the lot just received, not earliest expiry first. These are the goods bought for that order.
  - If that lot is held, or expired on `as_of`, it raises `LotUnavailable` and writes nothing, the receipt included.
- **Shipments** are records. A shipment is a number, its lines (reservation, quantity) and, once, a dispatch.
  - One shipment can carry several reservations. One reservation can ship across several shipments, but appears on each at most once.
  - `add_shipment_line` won't put more of a reservation on draft shipments than is left to ship.
  - **`dispatch` happens exactly once and needs a carrier and a label or tracking reference.**
    - It writes each line's movements as `ship` does: one movement for an item without lots, or one per lot by the lot rules. Then it writes the dispatch row, all in one transaction.
    - If any line can't ship, nothing is written and the shipment stays a draft.
    - Movement keys are `<key>/line:<line_id>` and, for lots, `<key>/line:<line_id>/lot:<lot_id>`. The movement's `source_ref` is `shipment:<number>`.
  - **Status is derived, not stored.**
    - A shipment is `delivered` once a `delivered` event exists, `dispatched` once its dispatch row exists, and `draft` otherwise.
    - Events are only ever appended, so a late event, whatever its `occurred_at`, can't move a delivered shipment back.
    - A stored status would have needed four triggers to keep it in step. Deriving it needs none, and it can't drift.
  - **Tracking events** take one of `in_transit`, `out_for_delivery`, `delivery_failed`, `delivered` or `exception`, with the carrier's UTC time (`YYYY-MM-DDTHH:MM:SSZ`) and optional detail. They are only accepted for a dispatched shipment. Nothing they write touches stock.

### How `ship` relates to `dispatch`

Both ship from a reservation through the same code and the same database rules: the same movements, the same `quantity_shipped` trigger, and the same lot rules.
- **`ship`** ships now and keeps no record beyond the movement. Its `source_ref` is `reservation:<id>`. Use it where no carrier is involved, such as a counter pickup or an internal transfer out, or for callers that don't track shipments yet.
- **`dispatch`** ships a planned shipment. Its movements carry `shipment_id` and `shipment_line_id`, so each line knows the movements it created.

They can be mixed on one reservation. Whatever `ship` takes is no longer available to a planned line. If a draft line then asks for more than is left, `dispatch` raises `OverShipment` and writes nothing. `ship` stays so that existing callers keep working. New code that sends goods with a carrier should use `dispatch`.

## Tables

| Table | Rows | Notes |
|---|---|---|
| `stock_movements` | One per receipt or shipment | Signed, non-zero quantity; `receipt` is positive, `shipment` is negative. A shipment carries its `reservation_id`; a receipt carries none. Immutable. |
| `stock_reservations` | One per reservation that reserved something | Status moves only `active` → `fulfilled` or `active` → `released`. Only `status` and `quantity_shipped` ever change, and `quantity_shipped` only through a shipment. Rows are never deleted. |
| `stock_commands` | One per successful command | The idempotency log. Immutable. |
| `stock_lot_tracking` | One per lot-tracked item | Immutable. |
| `stock_lots` | One per business, item and lot number | `expiry_date` may be NULL. Immutable. |
| `stock_lot_holds` | One per hold or release of a lot | Event, reason and key. The latest row says whether the lot is on hold. Immutable. |
| `stock_allocations` | One per lot per reservation of a tracked item | Quantity and the `as_of` date it was checked against. Written in the same transaction as its reservation, and before it. Immutable. |
| `stock_lot_levels` (view) | One per lot | `on_hand`, `allocated` (unshipped, on active reservations) and `on_hold`. `stock.py` and the triggers read the same view, so they share one definition. |

| `stock_shipments` | One per shipment | Number, unique per business. Immutable. |
| `stock_shipment_lines` | One per reservation per shipment | Quantity. Added only while the shipment is a draft. Immutable. |
| `stock_shipment_dispatches` | One per dispatched shipment | Carrier, label and tracking references, dispatch date. Its primary key makes dispatch happen once. Immutable. |
| `stock_shipment_events` | One per tracking event | Event, carrier time, detail. Only for a dispatched shipment. Immutable. |
| `stock_shipment_status` (view) | One per shipment | The derived status, with the dispatch details. |

`stock_movements` gained two columns in v2:
- `lot_id`: set exactly when the item is lot-tracked.
- `as_of`: the business date a lot shipment was checked against.

It gained two more in v3, plus the `opening` movement type:
- `shipment_id` and `shipment_line_id`: set together, only on a shipment movement written by `dispatch`. `as_of` is then the dispatch date.

How a shipment is tied to its reservation:
- **Foreign key:** `(business_id, reservation_id)` references `stock_reservations (business_id, id)`, so the reservation must exist in the same business.
- **CHECK:** a movement has a `reservation_id` exactly when it is a shipment.
- **Trigger:** the shipment's `item_id` and `location_id` must match its reservation's.

### Why quantity_shipped is stored

I kept `quantity_shipped` as a column instead of deriving it from the shipments. The database keeps it correct, so it can't drift from the ledger:
- An `AFTER INSERT` trigger on `stock_movements` adds each shipment to its reservation's `quantity_shipped`. When that reaches `quantity`, it sets `status = 'fulfilled'`.
- A `BEFORE UPDATE` trigger on `stock_reservations` rejects any row whose `quantity_shipped` differs from the sum of its shipments. Movements can't be updated or deleted, so the two can never diverge.
- `ship` doesn't touch the reservation. It only inserts the movement.

Deriving it instead would have cost clarity and some performance:
- **Two rules would move into triggers.** `quantity_shipped <= quantity` and "fulfilled iff fully shipped" are plain CHECKs on the row today; derived, each would need a trigger with a sum over movements.
- **Every read of reserved stock would need a join.** `reserved`, `available` and the reservation trigger (which runs on every reserve) would all join movements to reservations. Today each reads one table.
- **A reservation row would no longer show what has shipped.**

The cost of storing it: the shipping rules (raise shipped, fulfil when done) live in a trigger, and the app repeats the checks to give clear errors.

## Invariants and what enforces them

The database enforces each rule on its own: the "raw" tests write SQL directly and bypass `stock.py`. The app checks the same rules first so callers get a clear error, not an `IntegrityError`.

| Invariant | Database | App (`stock.py`) |
|---|---|---|
| Movements and the command log are never updated or deleted; reservations are never deleted | `BEFORE UPDATE` / `BEFORE DELETE` triggers | — |
| A receipt is positive, a shipment is negative, nothing is zero | `CHECK` on `movement_type` and the sign of `quantity` | Quantity must be a positive int |
| Every shipment belongs to a reservation in the same business, with the same item and location; receipts belong to none | Composite FK, `CHECK` and trigger (see [Tables](#tables)) | `ship` takes item and location from the reservation |
| `quantity_shipped` always equals the sum of the reservation's shipments | `AFTER INSERT` trigger on shipments, plus a `BEFORE UPDATE` guard | — |
| Shipped never exceeds reserved | `CHECK (quantity_shipped <= quantity)`, hit by the shipment's trigger | `OverShipment` |
| `fulfilled` means fully shipped, and fully shipped means `fulfilled` | `CHECK ((status = 'fulfilled') = (quantity_shipped = quantity))`; the shipment trigger sets it | — |
| A reservation starts `active` with nothing shipped; only `active` rows change (so shipping a released or fulfilled reservation fails); only `status` and `quantity_shipped` change | Triggers | `InvalidState` |
| Active reservations never exceed on hand | `BEFORE INSERT` trigger on `stock_reservations` | `reserve` caps at available, inside `BEGIN IMMEDIATE` |
| On hand never goes negative | Follows from the two rows above; also a `BEFORE INSERT` trigger on `stock_movements` | Shipments only come out of reservations |
| An idempotency key is used once per business | `UNIQUE (business_id, idempotency_key)` on all three tables | Replay lookup and payload comparison |
| Keys, refs and command names are not blank | `CHECK`s | `ValueError` |
| One business can't see or touch another's stock | The FK stops a shipment against another business's reservation | Every query and lookup filters by `business_id`; a reservation id from another business is `NotFound` |
| A command is all or nothing | — | One `BEGIN IMMEDIATE` transaction, rolled back on any error |

`BEGIN IMMEDIATE` takes SQLite's write lock before the availability read. A second `reserve` waits, then sees the first one's reservation. The insert triggers are a backstop for writes that bypass `stock.py`.

### Lot invariants (v2)

| Invariant | Database | App (`stock.py`) |
|---|---|---|
| Lot tracking starts before the item has stock, and never stops | `BEFORE INSERT` trigger; no-update and no-delete triggers | `InvalidState` |
| A tracked item's movements all carry a lot; an untracked item has no lots | Trigger on movements; FK from `stock_lots` to `stock_lot_tracking` | `LotMismatch` |
| A lot is unique per business, item and lot number; its number isn't blank; its expiry is a real date | `UNIQUE`, `CHECK`s (`x IS date(x)` rejects `2026-02-30` and non-ISO text) | `ValueError`, `LotMismatch` on a different expiry |
| A movement's or allocation's lot belongs to the same business and item | Composite FKs to `stock_lots (business_id, item_id, id)` | Lots are looked up by business and item |
| Lots, holds, allocations and the tracking list are never updated or deleted | `BEFORE UPDATE` / `BEFORE DELETE` triggers | — |
| **An allocation never exceeds its lot's free stock** | `BEFORE INSERT` trigger reading `stock_lot_levels`. Allocations whose reservation isn't written yet count too, so two in one transaction can't share the same stock | `reserve` allocates at most each lot's `free`, inside `BEGIN IMMEDIATE` |
| **A reservation's allocations add up to its quantity** (and are of its item); an untracked item's reservation has none | Allocations go in first, then the reservation's `AFTER INSERT` trigger checks the total. Allocations can't be added once the reservation exists (trigger) and can't change, and a deferred FK fails the COMMIT if the reservation never comes. So the rule holds for the reservation's whole life | `reserve` writes the reservation for exactly what it allocated |
| One allocation per reservation and lot | `UNIQUE (business_id, reservation_id, lot_id)`; it is also the parent key for lot shipments | Each lot appears once in the plan |
| Held lots and lots expired on `as_of` are never allocated | Triggers on allocations: lot on hold; `as_of` after expiry | `reserve` uses only `usable` lots |
| A lot shipment comes out of that reservation's allocation of that lot, and no more | FK from movements to `stock_allocations`; trigger on the allocation's remainder | `ship` takes at most each allocation's remainder |
| Nothing ships from a held lot or one expired on `as_of`; a lot shipment carries `as_of` | Triggers; `CHECK` | `ship` passes over those lots, `LotUnavailable` |
| A lot's on hand never goes negative | `BEFORE INSERT` trigger (implied by the rows above, see the results) | — |
| Holds alternate: hold, release, hold... with a reason | Trigger; `CHECK`s on event and reason; `UNIQUE` key | `InvalidState`, `ValueError` |
| One business can't see or touch another's lots | FK from holds to `stock_lots (business_id, id)` | Every lot lookup filters by `business_id`; another business's lot id is `NotFound` |

Two concurrent `reserve`s for the last units of a lot work the same way as in v1. `BEGIN IMMEDIATE` makes the second one wait and then read the first one's allocations. If a writer ever did read lots and write allocations in separate transactions, the within-lot trigger rejects the second one.

The database has no business clock. The expiry triggers trust the `as_of` written on the row. They guarantee that the stated date and the lot agree, not that the stated date is today.

### Opening stock and shipment invariants (v3)

| Invariant | Database | App (`stock.py`) |
|---|---|---|
| Opening stock is positive, has no reservation, and comes before any other movement of the item | Sign `CHECK`; reservation `CHECK`; `BEFORE INSERT` trigger | `InvalidState` |
| Receive-and-reserve is all or nothing | — (one transaction) | One `BEGIN IMMEDIATE` transaction; `LotUnavailable` before anything is kept |
| Shipment numbers are unique per business and not blank | `UNIQUE`, `CHECK` | `InvalidState` |
| A line belongs to a shipment and a reservation of the same business; a reservation is on a shipment at most once; quantity is positive | Composite FKs, `UNIQUE`, `CHECK` | `NotFound`, `InvalidState`, `ValueError` |
| Lines are added only before dispatch | `BEFORE INSERT` trigger | `InvalidState` |
| A line asks no more than is left to ship, counting other draft lines | — (checked again at dispatch, see the next row) | `OverShipment` |
| **Dispatch happens exactly once** | `PRIMARY KEY (business_id, shipment_id)` on dispatches. Shipment movements can't be written once the dispatch row exists (trigger). | `InvalidState`, inside `BEGIN IMMEDIATE` |
| **Dispatch creates the movements in the same transaction** | Dispatch's `BEFORE INSERT` trigger: the shipment has lines, and every line's movements add up to its quantity. Shipment movements have a deferred FK to the dispatch row, so they fail at COMMIT if it never comes. | `dispatch` writes them all, then the row |
| A dispatch has a carrier, a label or tracking reference and a date | `CHECK`s | `ValueError` |
| A shipment movement belongs to a line of that shipment and that reservation | Composite FK to `stock_shipment_lines (business_id, shipment_id, id, reservation_id)`; `CHECK`s: shipment and line set together, and only on shipments | `dispatch` writes them |
| Dispatch can't ship more than reserved, from a held or expired lot, or from a released reservation | The v1 and v2 shipment rules | `OverShipment`, `LotUnavailable`, `InvalidState` |
| Tracking events only for a dispatched shipment, from a fixed list, at a UTC time | FK to the dispatch row; `CHECK`s | `InvalidState`, `ValueError` |
| Tracking events never change stock, and delivered stays delivered | Events touch no stock table. Status is derived from rows that are only appended | — |
| Shipments, lines, dispatches and events are never updated or deleted | Triggers | — |
| One business can't see or touch another's shipments | Business-scoped FKs | `shipment()` filters by `business_id`; another business's shipment is `NotFound` |

## Running the tests

From the repo root, with the venv:

```
./venv/Scripts/python.exe -B modules/logistics/tests/test_stock.py       # Windows
./venv/Scripts/python.exe -B modules/logistics/tests/test_lots.py
./venv/Scripts/python.exe -B modules/logistics/tests/test_shipments.py
./venv/bin/python -B modules/logistics/tests/test_stock.py               # macOS / Linux
./venv/bin/python -B modules/logistics/tests/test_lots.py
./venv/bin/python -B modules/logistics/tests/test_shipments.py
```

Each test gets its own database in a temp directory. `-B` stops Python writing a `__pycache__` folder here. The tests never touch `operations_nerd/` or its database.

To check that the tests catch the removal of each protection (several minutes; it runs all three test files against each removal, 4 removals at a time, works on temp copies and never changes this folder):

```
./venv/Scripts/python.exe -B modules/logistics/tests/mutation_check.py   # Windows
./venv/bin/python -B modules/logistics/tests/mutation_check.py           # macOS / Linux
```

When you add a protection, add its removal to `MUTATIONS` in that script.

### Results

Run on 2026-10-23 with Python 3.11.9 and SQLite 3.45.1: **56 tests passed.** That is the 19 v1 tests in `test_stock.py` and the 20 lot tests in `test_lots.py`, both unchanged against the v3 schema and code, plus the 17 v3 tests in `test_shipments.py`.

The v3 tests:

| Test | Shows |
|---|---|
| opening stock is its own movement kind and comes first | `opening` movements, several allowed; refused after a receipt (app and raw); lot-tracked opening into a lot; raw: no negative opening, no reservation |
| receive and reserve is one transaction | Receipt and reservation together; replay; changed order, quantity or source conflicts; partial `reserve_quantity`; when the reservation insert fails, the receipt isn't kept either |
| receive and reserve a lot reserves that lot | Allocated from the new lot, not the earlier-expiring one; a second delivery of the lot; `as_of` required; a held or expired lot writes nothing, not even the lot |
| dispatch ships every line and records the shipment | Draft with nothing moved; dispatch writes one movement per line with shipment, line, date, key and `shipment:<number>`; status, carrier, refs; `shipment()` matches |
| dispatching twice changes stock once | Replay returns the stored result; a new key is `InvalidState` (dispatched, then delivered); raw: a second dispatch row and a late movement are rejected; stock unchanged |
| two concurrent dispatches of one shipment move stock once | Two threads, two keys, 5 trials, status read slowed. The reservation has room for a second dispatch, so only the once-only rules can stop it. Always one dispatch and one `InvalidState` |
| one customer line shipped in two shipments | 6 + 4 of a reservation of 10 on two shipments; a third line refused while both are drafts; fulfilled after the second dispatch |
| dispatch needs a carrier, a label or tracking reference, and shippable lines | Each input rule; no lines; a line that `ship` has since outgrown (nothing written); a released reservation; duplicate line; duplicate number; line after dispatch |
| ship keeps working beside dispatch | `ship` 3, dispatch 5 of one reservation; the two kinds of movement side by side |
| dispatch follows the lot rules | Dispatch passes over a held lot; refuses what only the held lot could cover (nothing written, still draft); refused the day after expiry; per-lot movement keys; recall |
| tracking events never change stock, and a delivered shipment stays delivered | Not on a draft; a late in-transit event after delivered leaves it delivered; stock rows identical before and after; replay; bad event and time formats |
| raw: shipments and lines are checked and immutable | Blank number or key, duplicate number or key, another business's reservation or shipment, zero quantity, a reservation twice; no update or delete |
| raw: a dispatch ships every line in full, once, and is immutable | No lines, nothing moved, a line left out, a line short, movements whose dispatch never comes; each dispatch `CHECK`; a movement with shipment but no line, a receipt on a line, another reservation's line; second dispatch; late movement; late line; key reuse; no update or delete |
| raw: tracking events are checked and immutable | Bad event, bad time formats, blank or reused key, another business; no update or delete; stock untouched |
| a reused key with different content is rejected | Create, line, dispatch (carrier, tracking, date), event (event, time), and across commands; nothing written |
| one business can't see or touch another's shipments | `shipment`, line, dispatch and event are `NotFound`; the same number is fine in another business; another business's reservation can't go on a line |
| bad shipment input is rejected before anything is written | Every new input; the connection is still usable |

The v1 tests:

| Test | Shows |
|---|---|
| receiving the same delivery twice with one key counts once | The replay returns the same result and writes nothing |
| replays of every command return the original result | Covers `reserve`, `ship` and `release`, including a replay after stock changed |
| same key with a different payload is rejected | Different quantity, ref, item or command; nothing written |
| two concurrent reserves for the last units | Two threads with two connections race for 5 units, 3 each, 10 times. Always one gets 3 and the other gets 2 with a shortage of 1. Reserved never exceeds on hand. The test slows the availability read to widen the race window. |
| reserve more than available returns the shortage | Includes the case with nothing available (no reservation row) and an item never received |
| partial receipt and partial shipment | One PO line received in two deliveries, shipped in two parts, then `fulfilled`; each shipment carries its `reservation_id` |
| shipping more than reserved is rejected | Rejected with nothing written; the key is still usable afterwards |
| release frees the unshipped remainder | Ship and release after release are rejected |
| on hand never goes negative | Through commands, and with raw inserts |
| one business can't see or touch another's stock | Same `item_id` and same idempotency key in two businesses |
| bad input is rejected before anything is written | The connection is left usable afterwards |
| raw: shipments belong to a reservation of the same business, item and location | Missing reservation, another business's reservation, wrong item, wrong location, and a receipt with a reservation are all rejected |
| raw: quantity_shipped always equals the shipments | Direct edits are rejected, and a raw shipment moves it |
| raw: keys unique per business; blanks rejected; sign matches type; reservations within on hand; status only moves forward; ledger rows can't be updated or deleted | Each remaining database rule on its own |

The lot tests:

| Test | Shows |
|---|---|
| items without lot tracking take no lot and keep the v1 results | A lot on an untracked item is refused; results have exactly the v1 keys; raw lots need a tracked item |
| lot tracking starts before any stock and never stops | Refused after a receipt; replay and re-tracking change nothing; tracking is per business; raw rows immutable |
| one PO line received as several lots; a lot is unique per business, item and number | Two lots and a second receipt of one lot under one PO line; a different expiry is refused; the same number on another item or business is another lot; no expiry allowed |
| raw: lots and lot movements are checked | Blank number, bad dates (`2026-02-30`, `2026/11/01`, ...), a tracked movement without a lot, another item's or business's lot, a bad `as_of`; lots immutable |
| reserve allocates earliest expiry first; one reservation can span lots | Five lots received out of order; ties go by receipt order, no expiry goes last; allocation rows store `as_of` |
| expired and held lots are never allocated; shortage returned | Expired yesterday is skipped, expiring today is used; a held lot is skipped until released; nothing usable gives no reservation |
| raw: allocations never come from held or expired lots | Each database rule, with the expiry date itself allowed |
| two concurrent reserves can't over-allocate one lot | Two threads race for a lot of 5, 3 each, 10 times, with the lot read slowed. The item also has 10 expired units, so the item-level rule can't be what stops it. Always 3 + 2, shortage 1 |
| an allocation never exceeds the lot's remaining stock | Checked after every command in a reserve/ship/release sequence; raw: over the lot, after shipping (frees nothing), after release (frees the rest), two pending reservations in one transaction |
| allocations always add up to the reservation | Raw: too little, too much, none, another item's lot, an untracked item, zero, bad `as_of`, a lot twice, a late allocation, a reservation that never comes or comes in another business, missing lot; allocations immutable |
| ship goes lot by lot | Two partial shipments across three lots; one movement per lot with `as_of` and key `<key>/lot:<id>`; fulfilled at the end |
| shipping from a held or expired lot is rejected | Ship passes over a held lot, refuses what only the held lot could cover and writes nothing; after release, refused the day after expiry, allowed on it |
| raw: lot shipments come out of their allocation from a usable lot | No lot, over the allocation, another item's lot, a lot with no allocation, missing `as_of`, expired, held |
| raw: lot on hand never goes negative | The redundant backstop, see below |
| holds replay, alternate and leave existing allocations in place | Replay, conflict, hold twice, release unheld; the allocation stays reserved; new reserves go around; release frees it; history kept with reasons |
| raw: holds alternate and are immutable | Release first, hold twice, blank reason or key, bad event, key reuse, another business's lot |
| recall lists every reservation and order a lot went to | A split line, a released reservation and an unshipped one; counts, status, on hold |
| lot commands replay and reject a changed payload | Receive, reserve and ship with lots; a changed lot, expiry or `as_of` conflicts |
| one business can't see or touch another's lots | Hold, release and recall are `NotFound`; `lots` is empty |
| bad lot input is rejected before anything is written | Dates as text only, ISO only; missing `as_of`; blank lot or reason; connection still usable |

**The mutation check:** 175 removals, 171 caught, 4 redundant, 0 survived, in under 9 minutes with 4 at a time. That is 46 v1 removals, 66 v2 and 63 v3. Each removal is one protection taken out of a temp copy, with all 56 tests run against it.

v3 results:
- **Caught (62 of 63):**
  - **Database:** every new `CHECK`, `UNIQUE` and foreign key. That includes the dispatch primary key, removed together with the two foreign keys that use it as their parent key; on its own, every insert would fail with "foreign key mismatch" and show nothing. Also every new trigger except the dispatch no-delete one, the no-lines part of the dispatch trigger on its own, the deferred dispatch FK made immediate (every dispatch fails), and the view's "delivered stays delivered".
  - **App:** every new check in `open_stock`, `receive_and_reserve`, `create_shipment`, `add_shipment_line`, `dispatch` and `record_tracking_event`, the business filter in `shipment()`, and timestamp validation.
  - **Concurrency:**
    - Reading the shipment's status outside the write lock: the race test sees "shipment movements are written before their dispatch" stop the second dispatch.
    - The same, with every once-only rule removed (primary key, that trigger, the every-line trigger): both threads dispatch and stock moves twice. This shows the race test really detects a double dispatch.
- **Redundant (1 new): the dispatch no-delete trigger.** Every dispatch row has shipment movements pointing at it through a foreign key, so it can't be deleted anyway. It is kept for its clearer message.
- The first full run found five test gaps, now fixed:
  - Two protections were rejected by a different rule first: shipments' no-update trigger (`UNIQUE` on the number) and the movement-to-line foreign key (the deferred dispatch FK, at the end of the statement).
  - The race test let the reservation stop the second dispatch.
  - Two removals were vacuous (the primary-key mismatch above).

v2 results:
- **Caught (all 66 new removals but one):**
  - **Database:** every new `UNIQUE`, `CHECK` and foreign key, including the parent keys. Every new trigger except the lot on-hand one. The item part of the allocation-match trigger on its own. The deferred FK made immediate (every lot reserve fails). The view's counting of pending allocations.
  - **App:** both `track_lots` checks, the three receive checks, the lot paths in reserve and ship, earliest-expiry-first, no-expiry-last, passing over held and expired lots in reserve and in ship, the expiry-day boundary, `LotUnavailable`, both `as_of` requirements, the hold state check, both lot business filters and date validation.
  - **Concurrency:**
    - Reading the lots outside the write lock: the race test sees the within-lot trigger reject the second reserve.
    - The same, with that trigger removed too: both threads reserve 3 of a lot of 5. This shows the lot race test really detects over-allocation.
    - Running with no transaction at all now also breaks every lot reserve at the deferred FK. The database refuses a lot reservation that isn't one transaction.
- **Redundant (1 new): the lot on-hand-not-negative trigger.** A shipment can't exceed its allocation, and an allocation can't exceed its lot's free stock. So a lot can't go negative today. It is kept for future negative movements that come out of no allocation, as with the item-level trigger.
- The first run of this check found four test gaps, now fixed:
  - The lot race test couldn't yet be shown to catch over-allocation.
  - Three protections were rejected only by a different rule first: lots' no-delete trigger (an FK held the rows), the view's pending allocations (the transaction failed at COMMIT anyway), and allocations-before-reservation (`UNIQUE` fired first).

v1 results, unchanged:
- **Caught (44):** a bad write is accepted, or a command misbehaves.
  - **Database:** all 4 `UNIQUE` keys (one is the FK's parent key), 11 of the 12 `CHECK`s, the foreign key, 12 of the 13 triggers, and, separately, the fulfil step inside the shipment trigger.
  - **App:**
    - `PRAGMA foreign_keys` in `connect()`.
    - `BEGIN IMMEDIATE` → deferred `BEGIN`: the concurrent test fails with `database is locked`.
    - No transaction at all: the reservation trigger catches the oversell.
    - No transaction and no reservation trigger: both threads reserve 3 of 5 units. This shows the concurrent test really detects overselling.
    - The replay lookup, payload comparison, rollback, reserve cap, over-shipment check, status check, the three `business_id` filters, and both input validators.
- **Redundant (2):** both are kept for their clearer error messages. With either removed, the bad write is still rejected, just by another rule.
  - **The on-hand-not-negative trigger.** Now that every shipment comes out of an active reservation, and reservations never exceed on hand, on hand can't go negative anyway. It is also the rule future negative movements (adjustments, write-offs) will need.
  - **`CHECK (quantity > 0)` on reservations.** A zero-quantity active row already breaks "fulfilled iff fully shipped", and a negative one breaks `quantity_shipped <= quantity`.

The script fails if the redundant set ever changes. That check caught a real gap in this round: the delete test only tried reservations with shipments, which the new FK already protects.

## Handoff with the buying side

- **Buying → stock: `receive`.** `source_ref` is the purchase order line, for example `po:7/line:1`.
  - When a PO line arrives in several deliveries, call `receive` once per delivery, with the same `source_ref` and a different idempotency key each time. The goods-receipt or delivery-note id makes a natural key.
  - Receiving the same delivery twice with its key counts once.
- **Stock → buying: `reserve` returns `shortage`.** That is the quantity the order wanted but couldn't get. The buying side decides whether to raise a purchase order for it.
  - Stock doesn't remember shortages. Once the goods arrive, the order calls `reserve` again with a new key for the missing quantity.
  - Suggested key: the order line id plus an attempt number.
- **Goods bought for a waiting order: `receive_and_reserve`.** It takes the purchase reference as `source_ref` and the customer order line as `order_ref`. The receipt and the reservation are one transaction under one key. If only part of the delivery is for that line, `reserve_quantity` says how much, and the rest becomes free stock.
- **Other suggested keys:** the shipment id for `ship` and `dispatch`, a cancellation id for `release`, and the carrier's event id for `record_tracking_event`.

## Where this differs from the brief

- **A third table, `stock_commands`.** `release` writes no row of its own, a `reserve` with nothing available writes no reservation, and a reserve's shortage can't be worked out later from stored rows. A key also has to be unique per business across all commands. One idempotency log covers all of that. Movements and reservations still carry their own `idempotency_key` with `UNIQUE (business_id, idempotency_key)`.
- **`release` and `ship` also take `business_id`.** Without it, a reservation id alone would let one business release or ship another's reservation. The idempotency key also needs a business to be scoped to.
- **Two backstop triggers.** One keeps on hand from going negative (now redundant, see above). The other keeps active reservations within on hand.

v2 (lots):
- **"Lot tracking is optional per item" became a command, `track_lots`.** The module can't read an item's config, so something has to tell it. I chose an explicit command over "an item is tracked once it receives a lot": with that rule, one receipt without a lot would have left the item untracked for good.
- **`ship` passes over a held or expired lot instead of rejecting the whole shipment.** It never ships from such a lot, which is what the brief asks. But if the reservation has usable allocations in other lots, it ships from those. It rejects (`LotUnavailable`) only when the usable allocations can't cover the quantity. Without this, one held lot would block every order that touched it.
- **`expiry_date` is optional.** A lot without one never expires and is allocated last. Some lot-tracked goods are tracked for traceability only.
- **`release_hold` takes a reason too,** so each row of the hold history says why.
- **The `as_of` date is stored** on every allocation and every lot shipment. The database uses it to check expiry, and it records which business date each decision was made on.

v3 (opening stock and shipments). I had no copy of the LogisticsNerd schema proposal; it isn't in the repo. These choices follow the brief as written:
- **Shipment status is derived, not a stored column.** See [above](#opening-stock-receive-and-reserve-and-shipments-v3). `shipment()` and the `stock_shipment_status` view still give it as `draft`, `dispatched` or `delivered`.
- **Carrier, label and tracking references live on the dispatch row,** since they are what dispatch requires and they are set once. A draft shipment has none. They can't be added or changed after dispatch.
- **`dispatch` always takes an `as_of` date,** the dispatch date, even when no line is lot-tracked. `ship` still needs it only for lots.
- **`receive_and_reserve` takes an optional `reserve_quantity`,** for a delivery that is only partly for the waiting line. For a lot-tracked item it reserves the lot just received, not the earliest-expiring one.
- **Opening stock must come before any receipt or shipment of the item.** "Initial" taken literally. A later count that corrects stock is an adjustment, which doesn't exist yet.
- **Tracking events come from a fixed list** of five. Only `delivered` changes the status. Carrier-specific codes would be mapped onto these by the caller.
- **A reservation appears on a shipment at most once.** That makes "which line created this movement" unambiguous. To ship more of it, add it to another shipment.
- **Lines can't be removed from a draft shipment.** Lines are immutable like everything else. A wrong draft is left undispatched, and a new shipment is created.

## Known limits

- **Foreign keys depend on each connection.** SQLite only enforces them when the connection has turned them on. `stock.connect()` does; a connection opened another way doesn't. On such a connection, a raw shipment could point at a missing reservation or at another business's reservation. It would still lower on hand, but no reservation would record it. Every other rule is a `CHECK` or a trigger and always applies.
- **Single location.** Commands always write `location_id = 1`. Queries add up every location, while the triggers check per location. Transfers and per-location queries come later.
- **No catalog, no core links.** `item_id` and `business_id` are unchecked integers. Any id is accepted until the core provides items and businesses to reference.
- **Receipts and shipments only.** There are no adjustments, returns, write-offs or stock counts yet. Each will need its own movement type and rules; a negative adjustment won't come out of a reservation, so the on-hand trigger will matter again.
- **Shipping rules live in SQL.** A trigger raises `quantity_shipped` and sets `fulfilled`, and `stock.py` repeats the checks for clear errors. Changing how shipping works means changing both, and existing databases need a migration for the triggers.
- **A shipment's `source_ref` duplicates `reservation_id`.** It is still `reservation:<id>`, which now adds nothing. It could carry the shipment or carrier reference instead.
- **Shortages aren't stored as backorders.** Incoming stock isn't allocated to waiting orders automatically.
- **Writers queue up.** `BEGIN IMMEDIATE` serialises every writer on the database file, across all businesses. A writer waits up to 10 s, then gets `database is locked`. That suits v1 volumes; the core uses the same pattern.
- **Every check reads the whole history.** The triggers and queries sum the ledger for each item through an index on `(business_id, item_id, location_id)`, and the `quantity_shipped` guard sums each reservation's shipments through an index on `(business_id, reservation_id)`. These costs grow with history; a periodic snapshot would cap them later.
- **The idempotency log grows forever.** Keys never expire. Payloads are compared exactly, so `po:1` and `PO:1` count as different.
- **A failed command records nothing.** Retrying its key runs the command against the stock as it is then.
- **Not tested at scale.** It was tested with SQLite 3.45.1 only, on small data, with a two-thread race on one machine. There are no performance numbers.

### Limits added by lots (v2)

- **The module's tables have no migration path.**
  - `init_db` runs `CREATE TABLE IF NOT EXISTS`, which creates missing tables but never changes existing ones.
  - A database created by v1 would get the new lot tables, but its `stock_movements` would not get `lot_id`, `as_of` or the new foreign keys and `CHECK`. The new code would then fail on it.
  - Today that doesn't matter: nothing is deployed. It matters soon. Each business now gets its own private database, so every schema change has to be applied to every business's database, safely and in order.
  - The core's planned versioned migration system should cover the module's tables too, or the module needs its own version table and numbered migrations. Either way, a change to `stock_movements` means rebuilding the table, since SQLite can't add a foreign key or `CHECK` to an existing table. The triggers and the view would need the same care.
- **Inputs come from the caller, not from config.**
  - Whether an item is lot-tracked, its lot numbers, expiry dates, the business date and hold reasons are all command inputs. They will later come from config through the module interface.
  - Until then, nothing checks that a lot number or expiry date matches a config record. The P1 finding in the food distributor experiment still stands: the module has no way to find "the lot-tracked field" in a business's config.
- **The business date is trusted.** The database checks expiry against the `as_of` written on the row. A caller that passes a wrong date can allocate or ship a lot that is really expired.
- **Lot tracking is set once.** It must be turned on before the item's first receipt, and can't be turned off. An item that already has stock can't move to lots. That would need a stock count into lots, which is an adjustment, and there are no adjustments yet.
- **Holds don't move existing allocations.** That is a deliberate choice, see [Lots and expiry](#lots-and-expiry). To take orders off a held lot for good, the caller releases and re-reserves them.
- **`ship` picks lots itself.** It can't be told which allocated lot to ship from. It always goes earliest expiry first, past held and expired lots. A picker who grabs a different allocated lot can't record that.
- **No minimum shelf life.** Customer rules such as "at least 5 days left" (M5 in the experiment) aren't supported. Reserve only refuses lots that are expired on `as_of`.
- **No catch weight, no units.** Quantities are still positive integers in one unit per item (M4 and M6 in the experiment).
- **Expiry is a date, not a time.** There are no time zones and no use-by hour.
- **Lot shipments use derived keys.** One `ship` command writes one movement per lot, keyed `<idempotency_key>/lot:<lot_id>`. A caller that uses a key of that shape for another command could collide with it. The collision is rejected, not double-counted, since keys are unique per business.
- **Choosing the reservation id.** Allocations are written before their reservation, so `reserve` picks the reservation's id itself (`MAX(id) + 1`), under the write lock. A writer outside `stock.py` doing the same must also hold the write lock.
- **Every lot check reads the lot's history.** The view sums a lot's movements and allocations each time. This is indexed by lot, but it grows with history, like the item-level checks.

### Limits added by shipments (v3)

- **Every v3 change touches existing tables, so the migration gap is wider.** `stock_movements` gained two columns, two foreign keys and two `CHECK`s, and its type `CHECK` now allows `opening`. A database created by v1 or v2 can't take any of that without rebuilding the table (see [Limits added by lots](#limits-added-by-lots-v2)).
- **No cancelling a draft shipment, and no removing lines.** An abandoned draft keeps its lines, and they count against the reservation when planning other shipments. `release` on the reservation still works, and `dispatch` of that draft then fails.
- **Planning is checked only by the app.** The database doesn't stop draft lines from asking for more than is left to ship. It stops the dispatch instead. Mixing `ship` with planned lines can leave a draft that can no longer be dispatched.
- **No returns or failed deliveries.** A `delivery_failed` or `exception` event is only recorded. Nothing brings stock back, which would need a return movement type.
- **Event times are trusted.** The database checks their format, not that they are after the dispatch, or in order.
- **Recall doesn't show shipments yet.** It lists reservations per lot. The shipment numbers a lot left on can be read from `shipment_id` on its movements, but `recall` doesn't return them.
- **`dispatch` movement keys are derived** (`<key>/line:<id>[/lot:<id>]`), with the same caution as lot shipments.
- **The carrier is free text.** There is no carrier list, label generation or rate lookup. Those belong to a carrier integration.
