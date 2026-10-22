-- LogisticsNerd stock engine, v1.
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
    command         TEXT    NOT NULL CHECK (command IN ('receive', 'reserve', 'release', 'ship')),
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
    source_ref      TEXT    NOT NULL CHECK (source_ref <> ''),
    idempotency_key TEXT    NOT NULL CHECK (idempotency_key <> ''),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (business_id, idempotency_key),
    FOREIGN KEY (business_id, reservation_id) REFERENCES stock_reservations (business_id, id),
    CHECK ((movement_type = 'receipt' AND quantity > 0) OR (movement_type = 'shipment' AND quantity < 0)),
    CHECK ((movement_type = 'shipment') = (reservation_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS stock_movements_item ON stock_movements (business_id, item_id, location_id);
CREATE INDEX IF NOT EXISTS stock_movements_reservation ON stock_movements (business_id, reservation_id);

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
