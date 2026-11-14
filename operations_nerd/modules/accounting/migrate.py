"""Checksummed PostgreSQL migration, independent of SQLite PR migration files."""
import hashlib
from pathlib import Path

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .errors import AccountingError, Forbidden

SCHEMA = Path(__file__).with_name("schema.sql")

CHART = [
    ("1000", "Cash and bank", "asset", "cash", False),
    ("1100", "Accounts receivable", "asset", "accounts_receivable", False),
    ("1200", "Inventory", "asset", "inventory", False),
    ("1300", "Interfund clearing", "asset", "interfund_clearing", False),
    ("1500", "Equipment and capital assets", "asset", "capital", True),
    ("2000", "Accounts payable", "liability", "accounts_payable", False),
    ("3000", "Owner capital", "equity", "equity", False),
    ("3100", "Donor restricted net assets", "equity", "donor_restricted", False),
    ("3200", "Unrestricted net assets", "equity", "unrestricted_net_assets", False),
    ("4000", "Sales revenue", "income", "revenue", False),
    ("4100", "Grant revenue", "income", "grant_revenue", False),
    ("5000", "Operating expense", "expense", "expense", False),
    ("5100", "Cost of goods sold", "expense", "cost_of_goods", False),
]


def initialize_database(conn, business_key):
    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise AccountingError("Accounting migration needs an idle autocommit connection")
    if not isinstance(business_key, str) or not business_key:
        raise AccountingError("A trusted business identity is required")
    conn.row_factory = dict_row
    content = SCHEMA.read_text()
    checksum = hashlib.sha256(content.encode()).hexdigest()
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(1768842370,0)")
        conn.execute("CREATE SCHEMA IF NOT EXISTS accounting")
        conn.execute("""CREATE TABLE IF NOT EXISTS accounting.schema_migrations(
            version text PRIMARY KEY,checksum text NOT NULL,applied_at timestamptz NOT NULL DEFAULT now())""")
        existing = conn.execute("SELECT checksum FROM accounting.schema_migrations WHERE version='001_accounting'").fetchone()
        if existing:
            if existing["checksum"] != checksum:
                raise AccountingError("Accounting migration checksum differs; use a forward migration")
            identity = conn.execute("SELECT business_key FROM accounting.settings WHERE singleton").fetchone()
            if identity is None or identity["business_key"] != business_key:
                raise Forbidden("Accounting database identity does not match this business")
            return []
        conn.execute(content)
        conn.execute("INSERT INTO accounting.settings(singleton,business_key,currency) VALUES(true,%s,'USD')", (business_key,))
        conn.execute("INSERT INTO accounting.funds(code,name,restriction_type,is_default) VALUES('GEN','General','unrestricted',true)")
        for values in CHART:
            conn.execute("INSERT INTO accounting.accounts(code,name,account_type,role,is_capital) VALUES(%s,%s,%s,%s,%s)", values)
        conn.execute("INSERT INTO accounting.schema_migrations(version,checksum) VALUES('001_accounting',%s)", (checksum,))
    return ["001_accounting"]
