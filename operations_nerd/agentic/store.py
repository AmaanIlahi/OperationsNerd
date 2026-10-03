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
