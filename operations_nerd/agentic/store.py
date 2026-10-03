"""
Database access for the agentic CRM. Every business read takes the owning
account id, so a business that belongs to someone else is indistinguishable
from one that does not exist.
"""

import json
import sqlite3

AGENTIC_PACK = "agentic"
AGENTIC_PACK_VERSION = "1"


class EmailTaken(Exception):
    pass


def create_account(conn, email: str, password_hash: str) -> int:
    try:
        cur = conn.execute(
            "INSERT INTO accounts (email, password_hash) VALUES (?, ?)", (email, password_hash)
        )
    except sqlite3.IntegrityError:
        raise EmailTaken(email)
    return cur.lastrowid


def get_account_by_email(conn, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def create_business_with_version_1(conn, account_id: int, name: str, template_id: str | None,
                                   spec: dict) -> int:
    cur = conn.execute(
        """INSERT INTO businesses (name, industry_pack, pack_version, settings_json, owner_key, current_version)
           VALUES (?, ?, ?, '{}', ?, 1)""",
        (name, template_id or AGENTIC_PACK, AGENTIC_PACK_VERSION, str(account_id)),
    )
    business_id = cur.lastrowid
    conn.execute(
        """INSERT INTO spec_versions (business_id, version, spec_json, ops_json, source, approved_by)
           VALUES (?, 1, ?, '[]', 'template', ?)""",
        (business_id, json.dumps(spec), account_id),
    )
    return business_id


def _business_summary(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "template": row["industry_pack"] if row["industry_pack"] != AGENTIC_PACK else None,
        "current_version": row["current_version"],
        "created_at": row["created_at"],
    }


def list_businesses(conn, account_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM businesses
           WHERE owner_key = ? AND current_version IS NOT NULL
           ORDER BY id""",
        (str(account_id),),
    ).fetchall()
    return [_business_summary(r) for r in rows]


def _owned_business_row(conn, account_id: int, business_id: int):
    return conn.execute(
        "SELECT * FROM businesses WHERE id = ? AND owner_key = ? AND current_version IS NOT NULL",
        (business_id, str(account_id)),
    ).fetchone()


def get_business_with_spec(conn, account_id: int, business_id: int) -> dict | None:
    row = _owned_business_row(conn, account_id, business_id)
    if not row:
        return None
    version = conn.execute(
        "SELECT spec_json FROM spec_versions WHERE business_id = ? AND version = ?",
        (business_id, row["current_version"]),
    ).fetchone()
    return {**_business_summary(row), "spec": json.loads(version["spec_json"])}


def list_versions(conn, account_id: int, business_id: int) -> list[dict] | None:
    if not _owned_business_row(conn, account_id, business_id):
        return None
    rows = conn.execute(
        """SELECT version, source, proposal_id, approved_by, created_at, ops_json
           FROM spec_versions WHERE business_id = ? ORDER BY version""",
        (business_id,),
    ).fetchall()
    return [
        {"version": r["version"], "source": r["source"], "proposal_id": r["proposal_id"],
         "approved_by": r["approved_by"], "created_at": r["created_at"],
         "ops": json.loads(r["ops_json"])}
        for r in rows
    ]


# ---------- versions ----------

class StaleVersion(Exception):
    """The business moved to a newer version while a change was being made."""


def get_owned_business_row(conn, account_id: int, business_id: int):
    return _owned_business_row(conn, account_id, business_id)


def get_version(conn, account_id: int, business_id: int, version: int) -> dict | None:
    if not _owned_business_row(conn, account_id, business_id):
        return None
    r = conn.execute(
        """SELECT version, spec_json, ops_json, source, proposal_id, approved_by, created_at
           FROM spec_versions WHERE business_id = ? AND version = ?""",
        (business_id, version),
    ).fetchone()
    if not r:
        return None
    return {"version": r["version"], "spec": json.loads(r["spec_json"]), "ops": json.loads(r["ops_json"]),
            "source": r["source"], "proposal_id": r["proposal_id"],
            "approved_by": r["approved_by"], "created_at": r["created_at"]}


def append_version(conn, business_id: int, expected_version: int, spec: dict, ops: list,
                   source: str, account_id: int, proposal_id: int | None = None) -> int:
    """Adds version expected_version + 1 and makes it current. History is only
    ever appended to. The compare-and-set on current_version means two
    concurrent changes cannot both land on the same base."""
    new_version = expected_version + 1
    claimed = conn.execute(
        "UPDATE businesses SET current_version = ? WHERE id = ? AND current_version = ?",
        (new_version, business_id, expected_version),
    )
    if claimed.rowcount == 0:
        raise StaleVersion()
    conn.execute(
        """INSERT INTO spec_versions (business_id, version, spec_json, ops_json, source, proposal_id, approved_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (business_id, new_version, json.dumps(spec), json.dumps(ops), source, proposal_id, account_id),
    )
    return new_version


# ---------- proposals ----------

def create_proposal(conn, business_id: int, base_version: int, messages: list, reply: str,
                    ops: list, rejected_ops: list, impact: list, status: str, model: str | None,
                    input_tokens: int | None, output_tokens: int | None, latency_ms: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO proposals (business_id, base_version, messages_json, reply, ops_json,
                                  rejected_ops_json, impact_json, status, model, input_tokens,
                                  output_tokens, latency_ms, decided_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'pending' THEN NULL ELSE datetime('now') END)""",
        (business_id, base_version, json.dumps(messages), reply, json.dumps(ops),
         json.dumps(rejected_ops), json.dumps(impact), status, model, input_tokens,
         output_tokens, latency_ms, status),
    )
    return cur.lastrowid


def _proposal_dict(r) -> dict:
    return {
        "id": r["id"], "business_id": r["business_id"], "base_version": r["base_version"],
        "reply": r["reply"], "operations": json.loads(r["ops_json"]),
        "rejected_operations": json.loads(r["rejected_ops_json"]),
        "impact": json.loads(r["impact_json"]), "status": r["status"], "model": r["model"],
        "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
        "latency_ms": r["latency_ms"], "created_at": r["created_at"], "decided_at": r["decided_at"],
    }


def get_owned_proposal(conn, account_id: int, proposal_id: int) -> dict | None:
    r = conn.execute(
        """SELECT p.* FROM proposals p JOIN businesses b ON b.id = p.business_id
           WHERE p.id = ? AND b.owner_key = ? AND b.current_version IS NOT NULL""",
        (proposal_id, str(account_id)),
    ).fetchone()
    return _proposal_dict(r) if r else None


def set_proposal_status(conn, proposal_id: int, from_status: str, to_status: str) -> bool:
    """Moves a proposal out of `from_status` exactly once; False if it already moved."""
    cur = conn.execute(
        "UPDATE proposals SET status = ?, decided_at = datetime('now') WHERE id = ? AND status = ?",
        (to_status, proposal_id, from_status),
    )
    return cur.rowcount == 1


def recent_rejections(conn, business_id: int, limit: int = 3) -> list[dict]:
    """Operations refused in the business's latest proposals, with reasons,
    shown to the agent next turn so it can correct itself."""
    rows = conn.execute(
        """SELECT rejected_ops_json FROM proposals
           WHERE business_id = ? AND rejected_ops_json != '[]'
           ORDER BY id DESC LIMIT ?""",
        (business_id, limit),
    ).fetchall()
    out = []
    for r in reversed(rows):
        out.extend(json.loads(r["rejected_ops_json"]))
    return out
