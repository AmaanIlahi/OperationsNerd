-- LogisticsNerd stock engine, v3: v1, plus lots and expiry (v2), plus opening stock and
-- shipment records (v3). Each later version has its own section at the end.
-- Stock is a ledger: on hand is the sum of stock_movements, reserved is the
-- unshipped part of active stock_reservations. Nothing holds a running total
-- that a caller edits: quantity_shipped is kept equal to the reservation's
-- shipments by the triggers below. Every table carries business_id. item_id and
-- location_id are plain integers: items will come from the core later, and v1
-- uses a single location. Foreign keys need PRAGMA foreign_keys = ON on each
-- connection; stock.connect() sets it.

-- One row per command that succeeded. It makes keys unique per business across
-- all commands and lets a replay return the original result.
CREATE TABLE IF NOT EXISTS stock_commands (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    command         TEXT    NOT NULL CHECK (command IN ('receive', 'reserve', 'release', 'ship',
                                                            'track_lots', 'hold_lot', 'release_hold',
                                                            'open_stock', 'receive_and_reserve', 'create_shipment',
                                                            'add_shipment_line', 'dispatch', 'record_tracking_event')),
    payload         TEXT    NOT NULL,
    result          TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key)
);

CREATE TRIGGER IF NOT EXISTS stock_commands_no_update BEFORE UPDATE ON stock_commands
BEGIN SELECT RAISE(ABORT, 'stock_commands rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_commands_no_delete BEFORE DELETE ON stock_commands
BEGIN SELECT RAISE(ABORT, 'stock_commands rows are immutable'); END;

CREATE TABLE IF NOT EXISTS stock_reservations (
    id               INTEGER PRIMARY KEY,
    business_id      INTEGER NOT NULL,
    item_id          INTEGER NOT NULL,
    location_id      INTEGER NOT NULL,
    order_ref        TEXT    NOT NULL CHECK (order_ref <> ''),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    quantity_shipped INTEGER NOT NULL DEFAULT 0 CHECK (quantity_shipped <= quantity),
    status           TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'fulfilled', 'released')),
    idempotency_key  TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key),
    UNIQUE (business_id, id),                -- the parent key for stock_movements' business-scoped foreign key
    CHECK ((status = 'fulfilled') = (quantity_shipped = quantity))
);

CREATE INDEX IF NOT EXISTS stock_reservations_item ON stock_reservations (business_id, item_id, location_id, status);

CREATE TABLE IF NOT EXISTS stock_movements (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    item_id         INTEGER NOT NULL,
    location_id     INTEGER NOT NULL,
    quantity        INTEGER NOT NULL,
    movement_type   TEXT    NOT NULL,
    reservation_id  INTEGER,
    lot_id          INTEGER,                 -- set exactly when the item is lot-tracked
    as_of           TEXT    CHECK (as_of IS date(as_of)),   -- the business date of a lot shipment or a dispatch
    shipment_id      INTEGER,                -- set exactly when a dispatch wrote the movement
    shipment_line_id INTEGER,
    source_ref      TEXT    NOT NULL CHECK (source_ref <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key),
    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id),
    FOREIGN KEY (business_id, item_id, lot_id) REFERENCES stock_lots (business_id, item_id, id),
    FOREIGN KEY (business_id, reservation_id, lot_id) REFERENCES stock_allocations (business_id, reservation_id, lot_id),
    FOREIGN KEY (business_id, shipment_id, shipment_line_id, reservation_id)
        REFERENCES stock_shipment_lines (business_id, shipment_id, id, reservation_id),
    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK ((movement_type IN ('receipt', 'opening') AND quantity > 0) OR (movement_type = 'shipment' AND quantity < 0)),
    CHECK ((movement_type = 'shipment') = (reservation_id IS NOT NULL)),
    CHECK (movement_type <> 'shipment' OR lot_id IS NULL OR as_of IS NOT NULL),
    CHECK ((shipment_id IS NULL) = (shipment_line_id IS NULL)),
    CHECK (shipment_line_id IS NULL OR movement_type = 'shipment')
);

CREATE INDEX IF NOT EXISTS stock_movements_item ON stock_movements (business_id, item_id, location_id);
CREATE INDEX IF NOT EXISTS stock_movements_reservation ON stock_movements (business_id, reservation_id);
CREATE INDEX IF NOT EXISTS stock_movements_lot ON stock_movements (business_id, lot_id);

CREATE TRIGGER IF NOT EXISTS stock_movements_no_update BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_movements_no_delete BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements rows are immutable'); END;

-- On hand can never go below zero, whoever writes the row.
CREATE TRIGGER IF NOT EXISTS stock_movements_on_hand_not_negative BEFORE INSERT ON stock_movements
WHEN NEW.quantity < 0
 AND NEW.quantity + (SELECT COALESCE(SUM(quantity), 0) FROM stock_movements
                     WHERE business_id = NEW.business_id AND item_id = NEW.item_id
                       AND location_id = NEW.location_id) < 0
BEGIN SELECT RAISE(ABORT, 'on hand would go negative'); END;

-- A shipment takes the item and location of its reservation. (The foreign key
-- already makes the reservation exist in the same business.)
CREATE TRIGGER IF NOT EXISTS stock_movements_match_reservation BEFORE INSERT ON stock_movements
WHEN NEW.reservation_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM stock_reservations
             WHERE business_id = NEW.business_id AND id = NEW.reservation_id
               AND (item_id <> NEW.item_id OR location_id <> NEW.location_id))
BEGIN SELECT RAISE(ABORT, 'shipment item or location does not match its reservation'); END;

-- A shipment moves its reservation's quantity_shipped, and marks it fulfilled
-- when everything has shipped. Shipping a reservation that is no longer active,
-- or past its quantity, fails here and takes the movement with it.
CREATE TRIGGER IF NOT EXISTS stock_movements_ship_reservation AFTER INSERT ON stock_movements
WHEN NEW.movement_type = 'shipment'
BEGIN
    UPDATE stock_reservations
       SET quantity_shipped = quantity_shipped - NEW.quantity,
           status = CASE WHEN quantity_shipped - NEW.quantity = quantity THEN 'fulfilled' ELSE status END
     WHERE business_id = NEW.business_id AND id = NEW.reservation_id;
END;

-- Active reservations never hold more than is on hand, whoever writes the row.
CREATE TRIGGER IF NOT EXISTS stock_reservations_within_on_hand BEFORE INSERT ON stock_reservations
WHEN NEW.quantity + (SELECT COALESCE(SUM(quantity - quantity_shipped), 0) FROM stock_reservations
                     WHERE business_id = NEW.business_id AND item_id = NEW.item_id
                       AND location_id = NEW.location_id AND status = 'active')
   > (SELECT COALESCE(SUM(quantity), 0) FROM stock_movements
      WHERE business_id = NEW.business_id AND item_id = NEW.item_id AND location_id = NEW.location_id)
BEGIN SELECT RAISE(ABORT, 'reservation exceeds available stock'); END;

-- Only new reservations may be active: a row is born active.
CREATE TRIGGER IF NOT EXISTS stock_reservations_born_active BEFORE INSERT ON stock_reservations
WHEN NEW.status <> 'active' OR NEW.quantity_shipped <> 0
BEGIN SELECT RAISE(ABORT, 'a reservation starts active with nothing shipped'); END;

-- quantity_shipped always equals the sum of this reservation's shipments.
CREATE TRIGGER IF NOT EXISTS stock_reservations_shipped_matches_ledger BEFORE UPDATE ON stock_reservations
WHEN NEW.quantity_shipped <> (SELECT COALESCE(-SUM(quantity), 0) FROM stock_movements
                              WHERE business_id = OLD.business_id AND reservation_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'quantity_shipped must equal the shipments of this reservation'); END;

-- Status moves only active -> fulfilled or active -> released; terminal rows are frozen.
CREATE TRIGGER IF NOT EXISTS stock_reservations_status_flow BEFORE UPDATE ON stock_reservations
WHEN OLD.status <> 'active'
BEGIN SELECT RAISE(ABORT, 'reservation is no longer active'); END;

-- Only status and quantity_shipped change.
CREATE TRIGGER IF NOT EXISTS stock_reservations_fixed_columns BEFORE UPDATE ON stock_reservations
WHEN NEW.id IS NOT OLD.id OR NEW.business_id IS NOT OLD.business_id OR NEW.item_id IS NOT OLD.item_id
  OR NEW.location_id IS NOT OLD.location_id OR NEW.order_ref IS NOT OLD.order_ref
  OR NEW.quantity IS NOT OLD.quantity OR NEW.idempotency_key IS NOT OLD.idempotency_key
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'only status and quantity_shipped may change'); END;

CREATE TRIGGER IF NOT EXISTS stock_reservations_no_delete BEFORE DELETE ON stock_reservations
BEGIN SELECT RAISE(ABORT, 'stock_reservations rows are kept'); END;

-- ---------------------------------------------------------------- lots and expiry (v2)
-- Lot tracking is opt-in per item and starts before the item has any stock. A
-- tracked item's movements all carry a lot; other items' carry none and behave
-- as in v1. Each reservation of a tracked item is backed by allocations
-- (reservation, lot, quantity) that add up to its quantity. The allocations are
-- written first, in the same transaction, and the reservation's insert checks
-- the total; the deferred foreign key makes the transaction fail at COMMIT if
-- the reservation never comes. Dates are 'YYYY-MM-DD'. A lot is expired on any
-- date after its expiry_date. The database has no business clock, so it checks
-- expiry against the as_of date written on the allocation or shipment.

CREATE TABLE IF NOT EXISTS stock_lot_tracking (
    business_id INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (business_id, item_id)
);

-- Stock that is already there has no lot to put it in.
CREATE TRIGGER IF NOT EXISTS stock_lot_tracking_before_stock BEFORE INSERT ON stock_lot_tracking
WHEN EXISTS (SELECT 1 FROM stock_movements WHERE business_id = NEW.business_id AND item_id = NEW.item_id)
BEGIN SELECT RAISE(ABORT, 'item already has stock without lots'); END;

CREATE TRIGGER IF NOT EXISTS stock_lot_tracking_no_update BEFORE UPDATE ON stock_lot_tracking
BEGIN SELECT RAISE(ABORT, 'stock_lot_tracking rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_lot_tracking_no_delete BEFORE DELETE ON stock_lot_tracking
BEGIN SELECT RAISE(ABORT, 'stock_lot_tracking rows are immutable'); END;

CREATE TABLE IF NOT EXISTS stock_lots (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL,
    item_id     INTEGER NOT NULL,
    lot_number  TEXT    NOT NULL CHECK (lot_number <> ''),
    expiry_date TEXT    CHECK (expiry_date IS date(expiry_date)),   -- NULL: the lot does not expire
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, item_id, lot_number),
    UNIQUE (business_id, id),                -- the parent key for stock_lot_holds
    UNIQUE (business_id, item_id, id),       -- the parent key for movements and allocations: same business and item
    FOREIGN KEY (business_id, item_id) REFERENCES stock_lot_tracking (business_id, item_id)
);

CREATE TRIGGER IF NOT EXISTS stock_lots_no_update BEFORE UPDATE ON stock_lots
BEGIN SELECT RAISE(ABORT, 'stock_lots rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_lots_no_delete BEFORE DELETE ON stock_lots
BEGIN SELECT RAISE(ABORT, 'stock_lots rows are immutable'); END;

-- A lot's hold history. The latest event says whether it is on hold now.
CREATE TABLE IF NOT EXISTS stock_lot_holds (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    lot_id          INTEGER NOT NULL,
    event           TEXT    NOT NULL CHECK (event IN ('hold', 'release')),
    reason          TEXT    NOT NULL CHECK (reason <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key),
    FOREIGN KEY (business_id, lot_id) REFERENCES stock_lots (business_id, id)
);

CREATE INDEX IF NOT EXISTS stock_lot_holds_lot ON stock_lot_holds (business_id, lot_id, id);

-- Hold, release, hold, ...: never two of the same in a row, and the first is a hold.
CREATE TRIGGER IF NOT EXISTS stock_lot_holds_alternate BEFORE INSERT ON stock_lot_holds
WHEN NEW.event = COALESCE((SELECT event FROM stock_lot_holds WHERE business_id = NEW.business_id AND lot_id = NEW.lot_id
                           ORDER BY id DESC LIMIT 1), 'release')
BEGIN SELECT RAISE(ABORT, 'a lot is held and released in turn'); END;

CREATE TRIGGER IF NOT EXISTS stock_lot_holds_no_update BEFORE UPDATE ON stock_lot_holds
BEGIN SELECT RAISE(ABORT, 'stock_lot_holds rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_lot_holds_no_delete BEFORE DELETE ON stock_lot_holds
BEGIN SELECT RAISE(ABORT, 'stock_lot_holds rows are immutable'); END;

CREATE TABLE IF NOT EXISTS stock_allocations (
    id             INTEGER PRIMARY KEY,
    business_id    INTEGER NOT NULL,
    reservation_id INTEGER NOT NULL,
    item_id        INTEGER NOT NULL,
    lot_id         INTEGER NOT NULL,
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    as_of          TEXT    NOT NULL CHECK (as_of IS date(as_of)),   -- the business date the lot was checked against
    created_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, reservation_id, lot_id),   -- one row per lot per reservation; the parent key for lot shipments
    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (business_id, item_id, lot_id) REFERENCES stock_lots (business_id, item_id, id)
);

CREATE INDEX IF NOT EXISTS stock_allocations_lot ON stock_allocations (business_id, lot_id);

-- Per lot: on hand, allocated (still to ship on active reservations, or on one
-- whose allocations are being written) and on_hold. Free is on_hand - allocated.
-- stock.py reads the same view, so the app and the triggers share one definition.
CREATE VIEW IF NOT EXISTS stock_lot_levels AS
SELECT l.id AS lot_id, l.business_id, l.item_id, l.lot_number, l.expiry_date,
       (SELECT COALESCE(SUM(m.quantity), 0) FROM stock_movements m
         WHERE m.business_id = l.business_id AND m.lot_id = l.id) AS on_hand,
       (SELECT COALESCE(SUM(a.quantity), 0) FROM stock_allocations a
          LEFT JOIN stock_reservations r ON r.business_id = a.business_id AND r.id = a.reservation_id
         WHERE a.business_id = l.business_id AND a.lot_id = l.id AND (r.id IS NULL OR r.status = 'active'))
     + (SELECT COALESCE(SUM(m.quantity), 0) FROM stock_movements m
          JOIN stock_reservations r ON r.business_id = m.business_id AND r.id = m.reservation_id
         WHERE m.business_id = l.business_id AND m.lot_id = l.id AND r.status = 'active') AS allocated,
       COALESCE((SELECT h.event = 'hold' FROM stock_lot_holds h
                  WHERE h.business_id = l.business_id AND h.lot_id = l.id ORDER BY h.id DESC LIMIT 1), 0) AS on_hold
  FROM stock_lots l;

-- Allocations are written with their reservation, before it, and never added later.
CREATE TRIGGER IF NOT EXISTS stock_allocations_before_reservation BEFORE INSERT ON stock_allocations
WHEN EXISTS (SELECT 1 FROM stock_reservations WHERE business_id = NEW.business_id AND id = NEW.reservation_id)
BEGIN SELECT RAISE(ABORT, 'allocations are written before their reservation'); END;

CREATE TRIGGER IF NOT EXISTS stock_allocations_not_held BEFORE INSERT ON stock_allocations
WHEN (SELECT on_hold FROM stock_lot_levels WHERE lot_id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'cannot allocate from a held lot'); END;

CREATE TRIGGER IF NOT EXISTS stock_allocations_not_expired BEFORE INSERT ON stock_allocations
WHEN NEW.as_of > (SELECT expiry_date FROM stock_lots WHERE id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'cannot allocate from an expired lot'); END;

-- An allocation never exceeds what is free in its lot, whoever writes the row.
CREATE TRIGGER IF NOT EXISTS stock_allocations_within_lot BEFORE INSERT ON stock_allocations
WHEN NEW.quantity > (SELECT on_hand - allocated FROM stock_lot_levels WHERE lot_id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'allocation exceeds the free stock of its lot'); END;

CREATE TRIGGER IF NOT EXISTS stock_allocations_no_update BEFORE UPDATE ON stock_allocations
BEGIN SELECT RAISE(ABORT, 'stock_allocations rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_allocations_no_delete BEFORE DELETE ON stock_allocations
BEGIN SELECT RAISE(ABORT, 'stock_allocations rows are immutable'); END;

-- A reservation of a tracked item is exactly its allocations, all of its item; any
-- other reservation has none. Allocations can't be added afterwards or changed, so
-- this holds for the reservation's whole life.
CREATE TRIGGER IF NOT EXISTS stock_reservations_match_allocations AFTER INSERT ON stock_reservations
WHEN (SELECT COALESCE(SUM(quantity), 0) FROM stock_allocations
       WHERE business_id = NEW.business_id AND reservation_id = NEW.id AND item_id = NEW.item_id)
     <> CASE WHEN EXISTS (SELECT 1 FROM stock_lot_tracking WHERE business_id = NEW.business_id AND item_id = NEW.item_id)
             THEN NEW.quantity ELSE 0 END
  OR EXISTS (SELECT 1 FROM stock_allocations
              WHERE business_id = NEW.business_id AND reservation_id = NEW.id AND item_id <> NEW.item_id)
BEGIN SELECT RAISE(ABORT, 'allocations must match the reservation''s item and quantity'); END;

CREATE TRIGGER IF NOT EXISTS stock_movements_need_lot BEFORE INSERT ON stock_movements
WHEN NEW.lot_id IS NULL
 AND EXISTS (SELECT 1 FROM stock_lot_tracking WHERE business_id = NEW.business_id AND item_id = NEW.item_id)
BEGIN SELECT RAISE(ABORT, 'movements of a lot-tracked item need a lot'); END;

-- A lot shipment ships no more from the lot than this reservation's allocation of it.
-- (The foreign key to stock_allocations already makes that allocation exist.)
CREATE TRIGGER IF NOT EXISTS stock_movements_within_allocation BEFORE INSERT ON stock_movements
WHEN NEW.lot_id IS NOT NULL AND NEW.reservation_id IS NOT NULL
 AND -NEW.quantity > (SELECT quantity FROM stock_allocations
                       WHERE business_id = NEW.business_id AND reservation_id = NEW.reservation_id AND lot_id = NEW.lot_id)
                   + (SELECT COALESCE(SUM(quantity), 0) FROM stock_movements
                       WHERE business_id = NEW.business_id AND reservation_id = NEW.reservation_id AND lot_id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'shipment exceeds its allocation'); END;

CREATE TRIGGER IF NOT EXISTS stock_movements_not_held BEFORE INSERT ON stock_movements
WHEN NEW.movement_type = 'shipment' AND (SELECT on_hold FROM stock_lot_levels WHERE lot_id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'cannot ship from a held lot'); END;

CREATE TRIGGER IF NOT EXISTS stock_movements_not_expired BEFORE INSERT ON stock_movements
WHEN NEW.movement_type = 'shipment' AND NEW.as_of > (SELECT expiry_date FROM stock_lots WHERE id = NEW.lot_id)
BEGIN SELECT RAISE(ABORT, 'cannot ship from an expired lot'); END;

-- A lot's on hand never goes below zero. Implied today by the allocation rules;
-- kept for future negative movements that come out of no allocation. Created
-- last so it runs first and gives its own message.
CREATE TRIGGER IF NOT EXISTS stock_movements_lot_on_hand_not_negative BEFORE INSERT ON stock_movements
WHEN NEW.quantity < 0 AND NEW.lot_id IS NOT NULL
 AND NEW.quantity + (SELECT on_hand FROM stock_lot_levels WHERE lot_id = NEW.lot_id) < 0
BEGIN SELECT RAISE(ABORT, 'lot on hand would go negative'); END;

-- ---------------------------------------------------------------- opening stock and shipments (v3)
-- Opening stock is its own movement kind, written before anything else moves
-- for the item. A shipment is a record: a number, lines (reservation, quantity)
-- and, once, a dispatch with carrier, label or tracking reference and date.
-- Dispatch writes each line's shipment movements first, then the dispatch row,
-- whose insert checks that every line moved in full; the movements' deferred
-- foreign key to the dispatch row makes the transaction fail at COMMIT if the
-- dispatch never comes. Status is derived (stock_shipment_status), never stored:
-- delivered once a delivered event exists, else dispatched once the dispatch
-- row exists, else draft. Tracking events only append; nothing they write
-- touches stock.

-- Opening stock comes before any receipt or shipment of the item.
CREATE TRIGGER IF NOT EXISTS stock_movements_opening_first BEFORE INSERT ON stock_movements
WHEN NEW.movement_type = 'opening'
 AND EXISTS (SELECT 1 FROM stock_movements WHERE business_id = NEW.business_id AND item_id = NEW.item_id
                                             AND movement_type <> 'opening')
BEGIN SELECT RAISE(ABORT, 'opening stock comes before any other movement of the item'); END;

CREATE TABLE IF NOT EXISTS stock_shipments (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    shipment_number TEXT    NOT NULL CHECK (shipment_number <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, shipment_number),
    UNIQUE (business_id, idempotency_key),
    UNIQUE (business_id, id)                 -- the parent key for lines and the dispatch
);

CREATE TRIGGER IF NOT EXISTS stock_shipments_no_update BEFORE UPDATE ON stock_shipments
BEGIN SELECT RAISE(ABORT, 'stock_shipments rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipments_no_delete BEFORE DELETE ON stock_shipments
BEGIN SELECT RAISE(ABORT, 'stock_shipments rows are immutable'); END;

CREATE TABLE IF NOT EXISTS stock_shipment_lines (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    shipment_id     INTEGER NOT NULL,
    reservation_id  INTEGER NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, shipment_id, reservation_id),        -- a reservation is on a shipment at most once
    UNIQUE (business_id, shipment_id, id, reservation_id),    -- the parent key for the movements a line creates
    UNIQUE (business_id, idempotency_key),
    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipments (business_id, id),
    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id)
);

-- Lines are added while the shipment is a draft.
CREATE TRIGGER IF NOT EXISTS stock_shipment_lines_before_dispatch BEFORE INSERT ON stock_shipment_lines
WHEN EXISTS (SELECT 1 FROM stock_shipment_dispatches WHERE business_id = NEW.business_id AND shipment_id = NEW.shipment_id)
BEGIN SELECT RAISE(ABORT, 'lines are added before dispatch'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipment_lines_no_update BEFORE UPDATE ON stock_shipment_lines
BEGIN SELECT RAISE(ABORT, 'stock_shipment_lines rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipment_lines_no_delete BEFORE DELETE ON stock_shipment_lines
BEGIN SELECT RAISE(ABORT, 'stock_shipment_lines rows are immutable'); END;

CREATE INDEX IF NOT EXISTS stock_movements_shipment_line ON stock_movements (business_id, shipment_line_id);

-- One per dispatched shipment: dispatch happens exactly once. No foreign key to
-- stock_shipments: the trigger below requires the shipment to have lines, and
-- lines have that foreign key, so it could never be the rule that rejects.
CREATE TABLE IF NOT EXISTS stock_shipment_dispatches (
    business_id     INTEGER NOT NULL,
    shipment_id     INTEGER NOT NULL,
    carrier         TEXT    NOT NULL CHECK (carrier <> ''),
    label_ref       TEXT    CHECK (label_ref <> ''),
    tracking_ref    TEXT    CHECK (tracking_ref <> ''),
    as_of           TEXT    NOT NULL CHECK (as_of IS date(as_of)),   -- the dispatch date
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (business_id, shipment_id),
    UNIQUE (business_id, idempotency_key),
    CHECK (label_ref IS NOT NULL OR tracking_ref IS NOT NULL)
);

-- A shipment movement is written by its dispatch, before the dispatch row, never after.
CREATE TRIGGER IF NOT EXISTS stock_movements_before_dispatch BEFORE INSERT ON stock_movements
WHEN NEW.shipment_id IS NOT NULL
 AND EXISTS (SELECT 1 FROM stock_shipment_dispatches WHERE business_id = NEW.business_id AND shipment_id = NEW.shipment_id)
BEGIN SELECT RAISE(ABORT, 'shipment movements are written before their dispatch'); END;

-- A dispatch ships a shipment that has lines, and every line in full.
CREATE TRIGGER IF NOT EXISTS stock_shipment_dispatches_ship_every_line BEFORE INSERT ON stock_shipment_dispatches
WHEN NOT EXISTS (SELECT 1 FROM stock_shipment_lines WHERE business_id = NEW.business_id AND shipment_id = NEW.shipment_id)
  OR EXISTS (SELECT 1 FROM stock_shipment_lines l
              WHERE l.business_id = NEW.business_id AND l.shipment_id = NEW.shipment_id
                AND l.quantity <> (SELECT COALESCE(-SUM(m.quantity), 0) FROM stock_movements m
                                    WHERE m.business_id = l.business_id AND m.shipment_line_id = l.id))
BEGIN SELECT RAISE(ABORT, 'a dispatch ships every line of its shipment in full'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipment_dispatches_no_update BEFORE UPDATE ON stock_shipment_dispatches
BEGIN SELECT RAISE(ABORT, 'stock_shipment_dispatches rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipment_dispatches_no_delete BEFORE DELETE ON stock_shipment_dispatches
BEGIN SELECT RAISE(ABORT, 'stock_shipment_dispatches rows are immutable'); END;

-- Carrier tracking events, append-only. Only a dispatched shipment has any (the
-- foreign key is to the dispatch row).
CREATE TABLE IF NOT EXISTS stock_shipment_events (
    id              INTEGER PRIMARY KEY,
    business_id     INTEGER NOT NULL,
    shipment_id     INTEGER NOT NULL,
    event           TEXT    NOT NULL CHECK (event IN ('in_transit', 'out_for_delivery', 'delivery_failed',
                                                      'delivered', 'exception')),
    occurred_at     TEXT    NOT NULL CHECK (occurred_at IS strftime('%Y-%m-%dT%H:%M:%SZ', occurred_at)),
    detail          TEXT,
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key),
    FOREIGN KEY (business_id, shipment_id) REFERENCES stock_shipment_dispatches (business_id, shipment_id)
);

CREATE INDEX IF NOT EXISTS stock_shipment_events_shipment ON stock_shipment_events (business_id, shipment_id);

CREATE TRIGGER IF NOT EXISTS stock_shipment_events_no_update BEFORE UPDATE ON stock_shipment_events
BEGIN SELECT RAISE(ABORT, 'stock_shipment_events rows are immutable'); END;

CREATE TRIGGER IF NOT EXISTS stock_shipment_events_no_delete BEFORE DELETE ON stock_shipment_events
BEGIN SELECT RAISE(ABORT, 'stock_shipment_events rows are immutable'); END;

-- A shipment with its derived status and dispatch details. A delivered event
-- arriving at any point makes it delivered for good; later events can't undo that.
CREATE VIEW IF NOT EXISTS stock_shipment_status AS
SELECT s.id AS shipment_id, s.business_id, s.shipment_number,
       CASE WHEN EXISTS (SELECT 1 FROM stock_shipment_events e
                          WHERE e.business_id = s.business_id AND e.shipment_id = s.id AND e.event = 'delivered')
            THEN 'delivered'
            WHEN d.shipment_id IS NOT NULL THEN 'dispatched'
            ELSE 'draft' END AS status,
       d.carrier, d.label_ref, d.tracking_ref, d.as_of AS dispatched_on
  FROM stock_shipments s
  LEFT JOIN stock_shipment_dispatches d ON d.business_id = s.business_id AND d.shipment_id = s.id;
