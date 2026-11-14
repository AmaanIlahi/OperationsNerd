"""Run the integrated local prototype without touching existing databases.

    .venv/bin/python scripts/run_integrated.py

With OPERATIONS_DATABASE_URL configured, use that PostgreSQL control database.
Otherwise a private development cluster is kept under ignored .local/postgres.
The account invitation is 'local-demo' unless explicitly configured. Model calls
default to the stub provider; configure a supported provider/key for live chat.
"""
import argparse
import hashlib
import os
from pathlib import Path
import secrets
import sys
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    local = ROOT / ".local"
    local.mkdir(mode=0o700, exist_ok=True)
    server = None
    if not os.getenv("OPERATIONS_DATABASE_URL"):
        try:
            import pgserver
        except ImportError:
            raise SystemExit("Install requirements-dev.txt for local PostgreSQL, or configure OPERATIONS_DATABASE_URL")
        import psycopg
        from urllib.parse import quote, urlencode, urlsplit, urlunsplit
        data = local / "postgres"
        if not (data / "PG_VERSION").exists():
            data.mkdir(mode=0o700, exist_ok=True)
            pgserver.initdb(["-A", "trust", "-U", "postgres"], pgdata=data)
        # pgserver's convenience launcher does not quote socket paths with
        # spaces. Use its bundled native tools and a short private socket path;
        # persistent database files stay inside this checkout's ignored .local.
        tag = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        sockets = Path(tempfile.gettempdir()) / ("opsnerd-socket-" + tag)
        sockets.mkdir(mode=0o700, exist_ok=True)
        try:
            pgserver.pg_ctl(["status"], pgdata=data)
        except subprocess.CalledProcessError:
            pgserver.pg_ctl(["-w", "-l", str(data / "log"), "-o", '-h "" -k ' + str(sockets), "start"], pgdata=data)
            server = (pgserver, data)
        admin_url = urlunsplit(("postgresql", "postgres@", "/postgres", urlencode({"host": str(sockets)}), ""))
        with psycopg.connect(admin_url, autocommit=True) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname='opsnerd_control'").fetchone():
                conn.execute('CREATE DATABASE opsnerd_control TEMPLATE template0')
        parsed = urlsplit(admin_url)
        os.environ["OPERATIONS_DATABASE_URL"] = urlunsplit((parsed.scheme, parsed.netloc, "/opsnerd_control", parsed.query, ""))
        os.environ["OPERATIONS_ADMIN_DATABASE_URL"] = admin_url
        import json
        configuration=local/'database.json'
        configuration.write_text(json.dumps({'control_url':os.environ['OPERATIONS_DATABASE_URL'],'admin_url':admin_url}))
        configuration.chmod(0o600)
    secret_path = local / "session-secret"
    if not secret_path.exists():
        secret_path.write_text(secrets.token_hex(32))
        secret_path.chmod(0o600)
    os.environ.setdefault("SESSION_SECRET", secret_path.read_text().strip())
    os.environ.setdefault("SIGNUP_INVITE_CODE", "local-demo")
    os.environ.setdefault("LLM_PROVIDER", "stub")
    # A 1 MiB CSV can expand when encoded as JSON. The import service retains
    # its own 1 MiB decoded-file cap; the transport has room for that envelope.
    os.environ.setdefault("MAX_BODY_BYTES", str(8 * 1024 * 1024))
    os.environ["OPERATIONS_UMBRELLA_ENABLED"] = "1"
    os.chdir(ROOT / "operations_nerd")
    sys.path.insert(0, str(ROOT / "operations_nerd"))
    import uvicorn
    print(f"Integrated OperationsNerd: http://127.0.0.1:{args.port}/app/", flush=True)
    print("Local signup invitation is configured; the default is local-demo.", flush=True)
    print(f"Chat provider: {os.environ['LLM_PROVIDER']}. No existing Supabase database is used by the local fallback.", flush=True)
    try:
        uvicorn.run("main:app", host="127.0.0.1", port=args.port)
    finally:
        # Retain the handle for the entire application lifetime. pgserver stops
        # the local server on normal process exit and preserves its data.
        if server:
            server[0].pg_ctl(["-w", "stop"], pgdata=server[1])


if __name__ == "__main__":
    main()
