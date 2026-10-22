# LogisticsNerd stock engine (v1)

The first slice of LogisticsNerd: receive stock, reserve it for orders, ship it and release what isn't needed. It is standalone Python 3.11 with stdlib `sqlite3` only. It imports nothing from `operations_nerd/` and makes no LLM calls.

Files:
- [stock.py](stock.py): the commands and queries
- [schema.sql](schema.sql): the tables, constraints and triggers
- [tests/test_stock.py](tests/test_stock.py): the tests
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

## Commands

Each command runs in one `BEGIN IMMEDIATE` transaction. `conn` comes from `stock.connect(path)`, which sets:
- autocommit mode, which the explicit transaction relies on
- `PRAGMA foreign_keys = ON`
- WAL and a 10 s busy timeout

| Command | What it does | Returns |
|---|---|---|
| `receive(conn, business_id, item_id, quantity, source_ref, idempotency_key)` | Adds stock: one positive `receipt` movement. `source_ref` is the purchase order line being received. | `movement_id`, `item_id`, `received` |
| `reserve(conn, business_id, item_id, quantity, order_ref, idempotency_key)` | Reserves up to what's available. If nothing is available, it writes no reservation and `reservation_id` is `None`. | `reservation_id`, `item_id`, `requested`, `reserved`, `shortage` |
| `release(conn, business_id, reservation_id, idempotency_key)` | Frees the unshipped remainder of an active reservation and marks it `released`. What already shipped stays shipped. | `reservation_id`, `released`, `status` |
| `ship(conn, business_id, reservation_id, quantity, idempotency_key)` | Ships part or all of an active reservation. It writes one negative `shipment` movement with `reservation_id` set. In the same statement, a database trigger raises the reservation's `quantity_shipped` and marks it `fulfilled` once everything has shipped. | `reservation_id`, `movement_id`, `shipped`, `quantity_shipped`, `remaining`, `status` |

Queries, per business and item: `on_hand`, `reserved` and `available`. `available` reads both sides in one statement.

Errors:
- `ValueError`: bad input, raised before anything is written.
- `IdempotencyConflict`: a key reused with a different payload.
- `NotFound`: the reservation isn't in this business.
- `InvalidState`: the reservation is no longer active.
- `OverShipment`: the shipment is more than is left to ship.

### Idempotency

Each successful command writes one row to `stock_commands` with its key, command name, payload and result:
- **Same key, same command and payload:** returns the stored result and writes nothing. This holds even if stock has changed since. A replayed `reserve` returns the original shortage, not a fresh one.
- **Same key, different command or payload:** raises `IdempotencyConflict`.
- **A command that fails records nothing,** so its key stays free for a corrected retry.

Keys are unique per business, so two businesses can use the same key.

## Tables

| Table | Rows | Notes |
|---|---|---|
| `stock_movements` | One per receipt or shipment | Signed, non-zero quantity; `receipt` is positive, `shipment` is negative. A shipment carries its `reservation_id`; a receipt carries none. Immutable. |
| `stock_reservations` | One per reservation that reserved something | Status moves only `active` → `fulfilled` or `active` → `released`. Only `status` and `quantity_shipped` ever change, and `quantity_shipped` only through a shipment. Rows are never deleted. |
| `stock_commands` | One per successful command | The idempotency log. Immutable. |

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

## Running the tests

From the repo root, with the venv:

```
./venv/Scripts/python.exe -B modules/logistics/tests/test_stock.py       # Windows
./venv/bin/python -B modules/logistics/tests/test_stock.py               # macOS / Linux
```

Each test gets its own database in a temp directory. `-B` stops Python writing a `__pycache__` folder here. The tests never touch `operations_nerd/` or its database.

To check that the tests catch the removal of each protection (about a minute; it works on temp copies and never changes this folder):

```
./venv/Scripts/python.exe -B modules/logistics/tests/mutation_check.py   # Windows
./venv/bin/python -B modules/logistics/tests/mutation_check.py           # macOS / Linux
```

When you add a protection, add its removal to `MUTATIONS` in that script.

### Results

Run on 2026-10-22 with Python 3.11.9 and SQLite 3.45.1: **19 tests passed.**

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

**The mutation check:** 46 removals, 44 caught, 2 redundant, 0 survived. Each removal is one protection taken out of a temp copy, with every test run against it.
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
- **Other suggested keys:** the shipment id for `ship`, and a cancellation id for `release`.

## Where this differs from the brief

- **A third table, `stock_commands`.** `release` writes no row of its own, a `reserve` with nothing available writes no reservation, and a reserve's shortage can't be worked out later from stored rows. A key also has to be unique per business across all commands. One idempotency log covers all of that. Movements and reservations still carry their own `idempotency_key` with `UNIQUE (business_id, idempotency_key)`.
- **`release` and `ship` also take `business_id`.** Without it, a reservation id alone would let one business release or ship another's reservation. The idempotency key also needs a business to be scoped to.
- **Two backstop triggers.** One keeps on hand from going negative (now redundant, see above). The other keeps active reservations within on hand.

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
