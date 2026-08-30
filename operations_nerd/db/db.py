"""
Thin database layer for Operations Nerd.

Design intent: this module knows nothing about any specific industry. It only
knows about the fixed core tables in schema.sql. Vertical-specific fields on
contacts and follow_ups are stored in a generalized entity_attributes table
(EAV pattern) and merged in/out transparently, so callers work with plain
dicts and never touch attribute rows directly.
"""

import sqlite3
import json
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "operations_nerd.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def init_db(db_path: str = DB_PATH, reset: bool = False):
    """Create the database and apply schema.sql. If reset=True, wipes any existing file first."""
    if reset and os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()


@contextmanager
def get_conn(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        # Any exception inside the with-block rolls back the whole transaction
        # so callers (notably the event pipeline's extract -> draft -> save ->
        # maybe_auto_approve sequence) get atomicity for free: either every
        # write lands or none of them do.
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row, json_fields=()) -> dict:
    d = dict(row)
    for field in json_fields:
        if field in d and d[field] is not None:
            d[field] = json.loads(d[field])
    return d


# ---------- businesses ----------

def create_business(conn, name: str, industry_pack: str, pack_version: str, settings: dict) -> int:
    cur = conn.execute(
        "INSERT INTO businesses (name, industry_pack, pack_version, settings_json) VALUES (?, ?, ?, ?)",
        (name, industry_pack, pack_version, json.dumps(settings)),
    )
    return cur.lastrowid


def get_business(conn, business_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    return _row_to_dict(row, json_fields=("settings_json",)) if row else None


# ---------- entity_attributes (generalized EAV) ----------
#
# Callers still work with plain dicts -- these are the only functions that
# know entity_attributes is a row-per-attribute table rather than a JSON
# column. That keeps the swap invisible to anything built on top.

def set_entity_attributes(conn, entity_type: str, entity_id: int, attributes: dict):
    """Upserts each key in `attributes` as a row. Values are JSON-encoded on write
    so type info (numbers, booleans, lists) survives the round trip."""
    for name, value in (attributes or {}).items():
        conn.execute(
            """INSERT INTO entity_attributes (entity_type, entity_id, attribute_name, value)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id, attribute_name)
               DO UPDATE SET value = excluded.value""",
            (entity_type, entity_id, name, json.dumps(value)),
        )


def get_entity_attributes(conn, entity_type: str, entity_id: int) -> dict:
    """Reassembles all attribute rows for one entity back into a dict."""
    rows = conn.execute(
        "SELECT attribute_name, value FROM entity_attributes WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    ).fetchall()
    return {r["attribute_name"]: json.loads(r["value"]) for r in rows}


def find_entity_ids_by_attribute(conn, entity_type: str, attribute_name: str, value) -> list[int]:
    """Exact-match filter across an attribute, e.g.
    find_entity_ids_by_attribute(conn, 'contact', 'property_type', 'condo').
    This is the query capability a JSON blob couldn't give us cleanly --
    the concrete payoff of the EAV switch."""
    rows = conn.execute(
        "SELECT entity_id FROM entity_attributes WHERE entity_type = ? AND attribute_name = ? AND value = ?",
        (entity_type, attribute_name, json.dumps(value)),
    ).fetchall()
    return [r["entity_id"] for r in rows]


# ---------- contacts ----------

def create_contact(conn, business_id: int, name: str, email: str = None,
                    phone: str = None, status: str = "active", extension_data: dict = None) -> int:
    cur = conn.execute(
        """INSERT INTO contacts (business_id, name, email, phone, status)
           VALUES (?, ?, ?, ?, ?)""",
        (business_id, name, email, phone, status),
    )
    contact_id = cur.lastrowid
    if extension_data:
        set_entity_attributes(conn, "contact", contact_id, extension_data)
    return contact_id


def get_contact(conn, contact_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if not row:
        return None
    contact = dict(row)
    contact["extension_data"] = get_entity_attributes(conn, "contact", contact_id)
    return contact


def list_contacts(conn, business_id: int) -> list[dict]:
    # One round-trip: aggregate EAV attributes into a JSON object per contact
    # via json_group_object, then LEFT JOIN it onto the contacts query. This
    # was previously N+1 (one query for the contact list, then one query per
    # contact for its attributes) -- the state endpoint called it on every
    # poll, so the multiplier was 3 + 3N round-trips for the full state view.
    rows = conn.execute(
        """SELECT c.*,
                  COALESCE(e.attrs_json, '{}') AS extension_data_json
           FROM contacts c
           LEFT JOIN (
               SELECT entity_id,
                      json_group_object(attribute_name, value) AS attrs_json
               FROM entity_attributes
               WHERE entity_type = 'contact'
               GROUP BY entity_id
           ) e ON e.entity_id = c.id
           WHERE c.business_id = ?
           ORDER BY c.created_at DESC""",
        (business_id,),
    ).fetchall()
    contacts = []
    for r in rows:
        c = dict(r)
        # EAV values are stored json-encoded (set_entity_attributes does this
        # so numbers / bools / lists round-trip). json_group_object preserves
        # the raw text, so we parse the outer object AND each inner value
        # to recover the original Python types -- same shape get_entity_attributes
        # returns when called per-row.
        c["extension_data"] = {
            k: json.loads(v) for k, v in json.loads(r["extension_data_json"]).items()
        }
        contacts.append(c)
    return contacts


# ---------- follow_ups ----------

def create_follow_up(conn, business_id: int, contact_id: int, type_: str,
                      due_date: str = None, notes: str = None, extension_data: dict = None) -> int:
    cur = conn.execute(
        """INSERT INTO follow_ups (business_id, contact_id, type, due_date, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (business_id, contact_id, type_, due_date, notes),
    )
    follow_up_id = cur.lastrowid
    if extension_data:
        set_entity_attributes(conn, "follow_up", follow_up_id, extension_data)
    return follow_up_id


def list_follow_ups(conn, business_id: int, status: str = None) -> list[dict]:
    # Same LEFT JOIN + json_group_object pattern as list_contacts --
    # previously N+1 here too, and the state endpoint called both.
    where = "WHERE f.business_id = ?"
    params: list = [business_id]
    if status:
        where += " AND f.status = ?"
        params.append(status)
    rows = conn.execute(
        f"""SELECT f.*,
                   COALESCE(e.attrs_json, '{{}}') AS extension_data_json
            FROM follow_ups f
            LEFT JOIN (
                SELECT entity_id,
                       json_group_object(attribute_name, value) AS attrs_json
                FROM entity_attributes
                WHERE entity_type = 'follow_up'
                GROUP BY entity_id
            ) e ON e.entity_id = f.id
            {where}
            ORDER BY f.due_date""",
        params,
    ).fetchall()
    follow_ups = []
    for r in rows:
        f = dict(r)
        # Same double-decode as list_contacts: outer JSON object from
        # json_group_object, inner json-encoded values from EAV storage.
        f["extension_data"] = {
            k: json.loads(v) for k, v in json.loads(r["extension_data_json"]).items()
        }
        follow_ups.append(f)
    return follow_ups


# ---------- events ----------

def create_event(conn, business_id: int, source: str, raw_content: str, contact_id: int = None) -> int:
    cur = conn.execute(
        "INSERT INTO events (business_id, contact_id, source, raw_content) VALUES (?, ?, ?, ?)",
        (business_id, contact_id, source, raw_content),
    )
    return cur.lastrowid


def mark_event_processed(conn, event_id: int, parsed_data: dict):
    conn.execute(
        "UPDATE events SET processed = 1, parsed_data = ? WHERE id = ?",
        (json.dumps(parsed_data), event_id),
    )


def get_event(conn, event_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row, json_fields=("parsed_data",)) if row else None


def list_events(conn, business_id: int) -> list[dict]:
    """All events for a business, any processed state. Used by the state view,
    not just the pending-work views."""
    rows = conn.execute(
        "SELECT * FROM events WHERE business_id = ? ORDER BY received_at", (business_id,)
    ).fetchall()
    return [_row_to_dict(r, json_fields=("parsed_data",)) for r in rows]


# ---------- drafted_actions ----------

def create_drafted_action(conn, event_id: int, business_id: int, action_type: str,
                           payload: dict, contact_id: int = None) -> int:
    cur = conn.execute(
        """INSERT INTO drafted_actions (event_id, business_id, contact_id, action_type, payload)
           VALUES (?, ?, ?, ?, ?)""",
        (event_id, business_id, contact_id, action_type, json.dumps(payload)),
    )
    return cur.lastrowid


def get_drafted_action(conn, action_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM drafted_actions WHERE id = ?", (action_id,)).fetchone()
    return _row_to_dict(row, json_fields=("payload",)) if row else None


def list_pending_actions(conn, business_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM drafted_actions WHERE business_id = ? AND status = 'pending_approval' ORDER BY created_at",
        (business_id,),
    ).fetchall()
    return [_row_to_dict(r, json_fields=("payload",)) for r in rows]


def list_drafted_actions(conn, business_id: int) -> list[dict]:
    """All drafted actions regardless of status. Used by the state view to
    show the full lifecycle (pending -> approved/rejected -> sent), not just
    what's currently waiting on a human."""
    rows = conn.execute(
        "SELECT * FROM drafted_actions WHERE business_id = ? ORDER BY created_at",
        (business_id,),
    ).fetchall()
    return [_row_to_dict(r, json_fields=("payload",)) for r in rows]


def set_action_status(conn, action_id: int, status: str, decided_at: bool = False):
    # decided_at is set only when a human actually made a decision
    # (approve_and_send). Auto-approval passes decided_at=False so the column
    # keeps its semantic: "when did a person decide on this action".
    if decided_at:
        conn.execute(
            "UPDATE drafted_actions SET status = ?, decided_at = datetime('now') WHERE id = ?",
            (status, action_id),
        )
    else:
        conn.execute(
            "UPDATE drafted_actions SET status = ? WHERE id = ?",
            (status, action_id),
        )


# ---------- approval_policies ----------

def set_approval_policy(conn, business_id: int, action_type: str, auto_approve: bool):
    conn.execute(
        """INSERT INTO approval_policies (business_id, action_type, auto_approve)
           VALUES (?, ?, ?)
           ON CONFLICT(business_id, action_type)
           DO UPDATE SET auto_approve = excluded.auto_approve, updated_at = datetime('now')""",
        (business_id, action_type, int(auto_approve)),
    )


def is_auto_approved(conn, business_id: int, action_type: str) -> bool:
    row = conn.execute(
        "SELECT auto_approve FROM approval_policies WHERE business_id = ? AND action_type = ?",
        (business_id, action_type),
    ).fetchone()
    return bool(row["auto_approve"]) if row else False


def list_approval_policies(conn, business_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM approval_policies WHERE business_id = ? ORDER BY action_type", (business_id,)
    ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db(reset=True)
    print(f"Initialized fresh database at {DB_PATH}")
