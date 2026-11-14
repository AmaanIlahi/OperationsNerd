"""Local-only test fixture that never resets a supplied or existing database."""

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


def database_url(admin_url, name):
    parts = urlsplit(admin_url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError("Integration tests require a PostgreSQL URL")
    query = dict(parse_qsl(parts.query))
    query.pop("dbname", None)
    return urlunsplit((parts.scheme, parts.netloc, "/" + quote(name), urlencode(query), ""))


class LocalDatabaseFixture:
    """Create one random control DB and track only its newly provisioned DBs.

    The caller must explicitly supply a local fixture URL. Database names that
    predated this fixture are never dropped, even if a registry row names one.
    """
    def __init__(self, admin_url):
        config = conninfo_to_dict(admin_url)
        for key in ("host", "hostaddr"):
            host = config.get(key, "")
            if "," in host or (host not in ("", "localhost", "127.0.0.1", "::1") and not host.startswith("/")):
                raise ValueError("HTTP acceptance tests require a local PostgreSQL fixture")
        self.admin_url = admin_url
        self.admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=10)
        self.original = {row[0] for row in self.admin.execute("SELECT datname FROM pg_database")}
        self.name = "operations_test_http_" + uuid4().hex[:16]
        self.admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(self.name)))
        self.url = database_url(admin_url, self.name)
        self.created = {self.name}

    def business_url(self, business_id):
        with psycopg.connect(self.url, autocommit=True) as conn:
            row = conn.execute("SELECT database_name FROM core.business_registry WHERE id=%s", (business_id,)).fetchone()
        if row is None or not re.fullmatch(r"opsnerd_biz_[0-9a-f]{32}", row[0]) or row[0] in self.original:
            raise AssertionError("The test registry did not identify a newly provisioned business database")
        self.created.add(row[0])
        return database_url(self.admin_url, row[0])

    def scalar(self, business_id, statement, values=()):
        with psycopg.connect(self.business_url(business_id), autocommit=True) as conn:
            return conn.execute(statement, values).fetchone()[0]

    def core_snapshot(self, business_id):
        with psycopg.connect(self.business_url(business_id), autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                tables = [row[0] for row in conn.execute("""SELECT tablename FROM pg_tables
                    WHERE schemaname='core' ORDER BY tablename""")]
                snapshot = {}
                for table in tables:
                    statement = sql.SQL("SELECT row_to_json(t)::text FROM core.{} t ORDER BY row_to_json(t)::text").format(sql.Identifier(table))
                    snapshot[table] = [row[0] for row in conn.execute(statement)]
                return snapshot

    def close(self):
        # Registration precedes CREATE DATABASE. This also finds databases from
        # an HTTP test that failed before business_url() could record the name.
        try:
            with psycopg.connect(self.url, autocommit=True) as conn:
                if conn.execute("SELECT to_regclass('core.business_registry')").fetchone()[0]:
                    for row in conn.execute("SELECT database_name FROM core.business_registry"):
                        if re.fullmatch(r"opsnerd_biz_[0-9a-f]{32}", row[0]) and row[0] not in self.original:
                            self.created.add(row[0])
            for name in sorted(self.created - self.original):
                self.admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        finally:
            self.admin.close()
