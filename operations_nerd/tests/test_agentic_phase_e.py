"""
Phase E tests: saved conversations, startup configuration, proxy-aware
cookies, rate limits, the daily chat cap, body size limit, /healthz,
the backup script and the container files.

Run like the others: python tests/test_agentic_phase_e.py from operations_nerd/.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager

ROOT = os.path.join(os.path.dirname(__file__), "..")
REPO = os.path.join(ROOT, "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(REPO, "scripts"))

from fastapi.testclient import TestClient
from starlette.requests import Request
from db import db as d
from main import app
from agentic import auth, chat, config, limits
from agentic.anthropic_llm import LLMResult, LLMError

os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
os.environ["RATE_LIMIT_ENABLED"] = "0"          # individual tests switch it on
d.init_db(reset=True)

PASSWORD = "correct horse battery"
_counter = [0]


@contextmanager
def env(**values):
    """Temporarily set (or with None, remove) environment variables."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def signup_body(email=None, code="let-me-in"):
    _counter[0] += 1
    return {"email": email or f"e{_counter[0]}@example.com", "password": PASSWORD, "invite_code": code}


def new_account(base_url="http://testserver") -> TestClient:
    c = TestClient(app, base_url=base_url)
    assert c.post("/api/auth/signup", json=signup_body()).status_code == 201
    return c


def stub(ops=None, reply="ok"):
    calls = []

    def fn(system, messages, schema):
        calls.append(messages)
        return LLMResult(json.dumps({"reply": reply, "operations": ops or []}), "stub", 5, 5, 1)
    fn.calls = calls
    return fn


def make_business(c, template="health_club"):
    r = c.post("/api/businesses", json={"name": "Biz", "template": template})
    assert r.status_code == 201, r.text
    return r.json()


def ask(c, bid, text="hello", history=None):
    msgs = (history or []) + [{"role": "user", "content": text}]
    return c.post(f"/api/businesses/{bid}/chat", json={"messages": msgs})


def request_with(headers=None, peer="10.0.0.9", scheme="http", host="example.test"):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()] + [(b"host", host.encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "scheme": scheme,
                    "server": (host, 80), "headers": raw, "client": (peer, 5555)})


# ---------- saved conversations ----------

def test_proposals_endpoint_returns_history_newest_last_and_is_scoped():
    c, other = new_account(), new_account()
    biz = make_business(c)
    bid = biz["id"]
    lead = next(e for e in biz["spec"]["entities"] if e["label"] == "Lead")["key"]
    original, chat.run_llm = chat.run_llm, stub([{"op": "add_field", "entity": lead, "label": "Ref", "type": "text"},
                                                  {"op": "add_field", "entity": lead, "label": "Colour", "type": "teleport"}], "Adding Ref")
    try:
        p1 = ask(c, bid, "first").json()
        history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "Adding Ref"}]
        p2 = ask(c, bid, "second", history).json()
    finally:
        chat.run_llm = original

    r = c.get(f"/api/businesses/{bid}/proposals")
    assert r.status_code == 200
    rows = r.json()
    assert [x["id"] for x in rows] == [p1["proposal_id"], p2["proposal_id"]]          # newest last
    first, second = rows
    assert first["messages"] == [{"role": "user", "content": "first"}]
    assert second["messages"][-1] == {"role": "user", "content": "second"} and len(second["messages"]) == 3
    assert first["reply"] == "Adding Ref" and first["status"] == "pending" and first["base_version"] == 1
    assert first["operations"] == [{"op": "add_field", "entity": lead, "label": "Ref", "type": "text", "required": False}]
    assert len(first["rejected_operations"]) == 1 and first["rejected_operations"][0]["reasons"]
    assert first["impact"][0]["op"] == "add_field" and first["impact"][0]["index"] == 0
    assert first["model"] == "stub" and first["input_tokens"] == 5

    assert len(c.get(f"/api/businesses/{bid}/proposals?limit=1").json()) == 1
    assert c.get(f"/api/businesses/{bid}/proposals?limit=1").json()[0]["id"] == p2["proposal_id"]   # the newest one
    assert other.get(f"/api/businesses/{bid}/proposals").status_code == 404
    assert TestClient(app).get(f"/api/businesses/{bid}/proposals").status_code == 401
    assert new_account().get("/api/businesses/999999/proposals").status_code == 404


def test_pending_proposal_can_be_approved_after_reload():
    email = "reload@example.com"
    c = TestClient(app)
    assert c.post("/api/auth/signup", json=signup_body(email)).status_code == 201
    biz = make_business(c)
    bid = biz["id"]
    lead = next(e for e in biz["spec"]["entities"] if e["label"] == "Lead")["key"]
    original, chat.run_llm = chat.run_llm, stub([{"op": "add_field", "entity": lead, "label": "Ref", "type": "text"}])
    try:
        pid = ask(c, bid).json()["proposal_id"]
    finally:
        chat.run_llm = original

    # "reload": a brand new browser session logs in and finds the pending proposal
    fresh = TestClient(app)
    assert fresh.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    pending = [p for p in fresh.get(f"/api/businesses/{bid}/proposals").json() if p["status"] == "pending"]
    assert [p["id"] for p in pending] == [pid]
    assert fresh.post(f"/api/proposals/{pid}/approve").status_code == 200
    assert fresh.get(f"/api/businesses/{bid}").json()["current_version"] == 2
    assert fresh.get(f"/api/businesses/{bid}/proposals").json()[0]["status"] == "approved"


def test_page_rebuilds_conversation_from_saved_proposals():
    html = open(os.path.join(ROOT, "frontend_app", "index.html"), encoding="utf-8").read()
    assert "/proposals`" in html and "rebuildConversation" in html and "loadChatHistory" in html
    assert "innerHTML" not in html


# ---------- startup configuration ----------

PROD_OK = dict(APP_ENV="production", SESSION_SECRET="s" * 40, ANTHROPIC_API_KEY="key", CONFIG_AGENT_ENABLED=None)


def test_production_requires_secret_and_api_key():
    with env(**PROD_OK):
        assert config.check_environment() == []
    with env(**{**PROD_OK, "SESSION_SECRET": None}):
        assert any("SESSION_SECRET is required" in p for p in config.check_environment())
    with env(**{**PROD_OK, "SESSION_SECRET": "short"}):
        assert any("at least 32" in p for p in config.check_environment())
    with env(**{**PROD_OK, "ANTHROPIC_API_KEY": None}):
        assert any("ANTHROPIC_API_KEY is required" in p for p in config.check_environment())
    with env(**{**PROD_OK, "CONFIG_AGENT_ENABLED": "1"}):
        assert any("CONFIG_AGENT_ENABLED" in p for p in config.check_environment())
    with env(**{**PROD_OK, "CONFIG_AGENT_ENABLED": "0"}):                     # "set" at all is refused
        assert any("CONFIG_AGENT_ENABLED" in p for p in config.check_environment())
    with env(**{**PROD_OK, "SESSION_SECRET": None, "ANTHROPIC_API_KEY": None, "CONFIG_AGENT_ENABLED": "1"}):
        assert len(config.check_environment()) == 3                           # all problems reported together
        try:
            config.startup()
        except config.ConfigError as e:
            assert "Refusing to start" in str(e)
        else:
            raise AssertionError("startup accepted a bad production config")


def test_development_mode_is_lenient_but_bad_numbers_are_caught():
    with env(APP_ENV=None, SESSION_SECRET=None, ANTHROPIC_API_KEY=None, CONFIG_AGENT_ENABLED="1",
             CHAT_DAILY_LIMIT=None, MAX_BODY_BYTES=None):
        assert config.check_environment() == []
        assert config.chat_daily_limit() == 50 and config.max_body_bytes() == 1024 * 1024
    with env(APP_ENV=None, CHAT_DAILY_LIMIT="lots", MAX_BODY_BYTES="-5"):
        problems = config.check_environment()
        assert len(problems) == 2 and config.chat_daily_limit() == 50
    with env(CHAT_DAILY_LIMIT="7"):
        assert config.chat_daily_limit() == 7


def _import_main(**environment):
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("APP_ENV", "SESSION_SECRET", "ANTHROPIC_API_KEY", "CONFIG_AGENT_ENABLED", "DATABASE_PATH")}
    child_env.update({k: v for k, v in environment.items() if v is not None})
    return subprocess.run([sys.executable, "-c", "import main; print('started', main.d_path if hasattr(main,'d_path') else '')"],
                          cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=120)


def test_app_refuses_to_start_in_production_with_bad_config():
    bad = _import_main(APP_ENV="production")
    assert bad.returncode != 0 and "Refusing to start" in bad.stderr and "SESSION_SECRET" in bad.stderr
    flagged = _import_main(APP_ENV="production", SESSION_SECRET="s" * 40, ANTHROPIC_API_KEY="k",
                           CONFIG_AGENT_ENABLED="1", DATABASE_PATH=os.path.join(tempfile.mkdtemp(), "x.db"))
    assert flagged.returncode != 0 and "CONFIG_AGENT_ENABLED" in flagged.stderr


def test_app_starts_with_database_path_from_env_and_creates_it():
    target = os.path.join(tempfile.mkdtemp(), "nested", "dir", "ops.db")
    ok = _import_main(APP_ENV="production", SESSION_SECRET="s" * 40, ANTHROPIC_API_KEY="k", DATABASE_PATH=target)
    assert ok.returncode == 0, ok.stderr
    assert os.path.isfile(target)                                           # directory and schema created
    conn = sqlite3.connect(target)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"accounts", "sessions", "spec_versions", "proposals", "chat_usage", "businesses"} <= tables


def test_default_database_path_is_unchanged():
    out = subprocess.run([sys.executable, "-c", "from db import db; print(db.DB_PATH)"], cwd=ROOT,
                         env={k: v for k, v in os.environ.items() if k != "DATABASE_PATH"},
                         capture_output=True, text=True)
    assert out.stdout.strip().endswith(os.path.join("db", "operations_nerd.db"))


# ---------- behind a reverse proxy ----------

def _cookie_secure(client, headers=None) -> bool:
    r = client.post("/api/auth/signup", json=signup_body(), headers=headers or {})
    assert r.status_code == 201, r.text
    return "secure" in r.headers["set-cookie"].lower().replace("samesite", "")


def test_forwarded_proto_only_trusted_from_the_proxy():
    https = {"X-Forwarded-Proto": "https"}
    with env(APP_ENV=None, TRUSTED_PROXIES=None):
        assert not _cookie_secure(TestClient(app), https)                   # header ignored: nobody is trusted
        assert not _cookie_secure(TestClient(app))
    with env(APP_ENV=None, TRUSTED_PROXIES="testclient"):                   # the test client's peer name
        assert _cookie_secure(TestClient(app), https)                       # trusted proxy says https
        assert not _cookie_secure(TestClient(app), {"X-Forwarded-Proto": "http"})
        assert _cookie_secure(TestClient(app), {"X-Forwarded-Proto": "https, http"})   # first hop wins
    with env(APP_ENV=None, TRUSTED_PROXIES="*"):
        assert _cookie_secure(TestClient(app), https)
    with env(APP_ENV=None, TRUSTED_PROXIES="10.0.0.0/8, 192.168.1.5"):      # someone else is the proxy
        assert not _cookie_secure(TestClient(app), https)


def test_cookie_is_always_secure_in_production_and_on_public_hosts():
    with env(APP_ENV="production", TRUSTED_PROXIES=None):
        assert _cookie_secure(TestClient(app))
        r = TestClient(app).post("/api/auth/logout")
        assert "secure" in r.headers["set-cookie"].lower()
    with env(APP_ENV=None):
        assert _cookie_secure(TestClient(app, base_url="https://crm.example.com"))
        assert not _cookie_secure(TestClient(app))                          # local development still works over http


def test_effective_scheme_and_client_ip_unit():
    with env(TRUSTED_PROXIES="10.0.0.0/8"):
        r = request_with({"X-Forwarded-Proto": "https"}, peer="10.1.2.3")
        assert auth.effective_scheme(r) == "https"
        assert auth.effective_scheme(request_with({"X-Forwarded-Proto": "https"}, peer="8.8.8.8")) == "http"
        # proxy chain: the real client is the right-most entry that is not a trusted proxy
        r = request_with({"X-Forwarded-For": "6.6.6.6, 1.2.3.4, 10.0.0.5"}, peer="10.0.0.9")
        assert auth.client_ip(r) == "1.2.3.4"                               # a spoofed left-most entry is ignored
        # an outsider cannot choose their own address
        assert auth.client_ip(request_with({"X-Forwarded-For": "1.1.1.1"}, peer="8.8.8.8")) == "8.8.8.8"
        assert auth.client_ip(request_with(peer="8.8.8.8")) == "8.8.8.8"
        assert auth.client_ip(request_with({"X-Forwarded-For": "1.2.3.4"}, peer="10.0.0.9")) == "1.2.3.4"
    with env(TRUSTED_PROXIES="*"):
        assert auth.client_ip(request_with({"X-Forwarded-For": "9.9.9.9, 1.2.3.4"}, peer="172.16.0.1")) == "1.2.3.4"
    with env(TRUSTED_PROXIES=None):
        assert auth.client_ip(request_with({"X-Forwarded-For": "1.2.3.4"}, peer="10.0.0.9")) == "10.0.0.9"


# ---------- rate limiting ----------

@contextmanager
def tight_limits(**overrides):
    saved = {k: getattr(limits, k) for k in overrides}
    try:
        for k, v in overrides.items():
            setattr(limits, k, v)
        limits.reset()
        with env(RATE_LIMIT_ENABLED="1"):
            yield
    finally:
        for k, v in saved.items():
            setattr(limits, k, v)
        limits.reset()


def test_login_rate_limit_per_email_and_success_clears_it():
    email = "limit@example.com"
    assert TestClient(app).post("/api/auth/signup", json=signup_body(email)).status_code == 201
    with tight_limits(LOGIN_MAX_FAILURES_PER_EMAIL=3):
        c = TestClient(app)
        bad = {"email": email, "password": "wrong password"}
        for _ in range(2):
            assert c.post("/api/auth/login", json=bad).status_code == 401
        # a success resets the counter
        assert c.post("/api/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
        for _ in range(3):
            assert c.post("/api/auth/login", json=bad).status_code == 401
        r = c.post("/api/auth/login", json=bad)
        assert r.status_code == 429 and "Too many" in r.json()["detail"] and int(r.headers["Retry-After"]) > 0
        # locked out even with the right password, and for every spelling of the email
        assert c.post("/api/auth/login", json={"email": email.upper(), "password": PASSWORD}).status_code == 429
        # other emails are unaffected
        assert c.post("/api/auth/login", json={"email": "someone-else@example.com", "password": "x" * 9}).status_code == 401


def test_login_rate_limit_per_ip_uses_forwarded_address_from_trusted_proxy():
    with tight_limits(LOGIN_MAX_PER_IP=3, LOGIN_MAX_FAILURES_PER_EMAIL=1000), env(TRUSTED_PROXIES="*"):
        c = TestClient(app)
        body = {"email": "nobody@example.com", "password": "x" * 9}
        from_a = {"X-Forwarded-For": "1.1.1.1"}
        for _ in range(3):
            assert c.post("/api/auth/login", json=body, headers=from_a).status_code == 401
        assert c.post("/api/auth/login", json=body, headers=from_a).status_code == 429
        assert c.post("/api/auth/login", json=body, headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 401   # another visitor
    with tight_limits(LOGIN_MAX_PER_IP=2, LOGIN_MAX_FAILURES_PER_EMAIL=1000), env(TRUSTED_PROXIES=None):
        c = TestClient(app)
        for i in range(2):          # without a trusted proxy a forged header cannot dodge the limit
            assert c.post("/api/auth/login", json=body, headers={"X-Forwarded-For": f"3.3.3.{i}"}).status_code == 401
        assert c.post("/api/auth/login", json=body, headers={"X-Forwarded-For": "3.3.3.99"}).status_code == 429


def test_signup_rate_limit_per_ip_and_email_covers_invite_guessing():
    with tight_limits(SIGNUP_MAX_PER_IP=4, SIGNUP_MAX_PER_EMAIL=2):
        c = TestClient(app)
        for _ in range(2):          # guessing the invite code from one email
            assert c.post("/api/auth/signup", json=signup_body("guess@example.com", code="nope")).status_code == 403
        assert c.post("/api/auth/signup", json=signup_body("guess@example.com", code="let-me-in")).status_code == 429
        for _ in range(2):          # the same address guessing with different emails
            assert c.post("/api/auth/signup", json=signup_body(code="nope")).status_code == 403
        r = c.post("/api/auth/signup", json=signup_body(code="nope"))
        assert r.status_code == 429 and "Retry-After" in r.headers
    # off switch
    with tight_limits(SIGNUP_MAX_PER_IP=1, SIGNUP_MAX_PER_EMAIL=1), env(RATE_LIMIT_ENABLED="0"):
        c = TestClient(app)
        for _ in range(3):
            assert c.post("/api/auth/signup", json=signup_body(code="nope")).status_code == 403


def test_limiter_window_expires():
    import time
    with tight_limits(SIGNUP_WINDOW=0.2, SIGNUP_MAX_PER_IP=1, SIGNUP_MAX_PER_EMAIL=5):
        c = TestClient(app)
        assert c.post("/api/auth/signup", json=signup_body(code="nope")).status_code == 403
        assert c.post("/api/auth/signup", json=signup_body(code="nope")).status_code == 429
        time.sleep(0.3)
        assert c.post("/api/auth/signup", json=signup_body(code="nope")).status_code == 403


# ---------- daily chat cap ----------

def test_daily_chat_cap_returns_clear_429_and_makes_no_model_call():
    c, other = new_account(), new_account()
    biz, other_biz = make_business(c), make_business(other)
    fake = stub()
    original, chat.run_llm = chat.run_llm, fake
    try:
        with env(CHAT_DAILY_LIMIT="2"):
            assert ask(c, biz["id"]).status_code == 200
            assert ask(c, biz["id"]).status_code == 200
            calls_before = len(fake.calls)
            r = ask(c, biz["id"])
            assert r.status_code == 429
            assert "all 2 assistant messages" in r.json()["detail"] and "00:00 UTC" in r.json()["detail"]
            assert len(fake.calls) == calls_before                            # nothing was spent
            assert ask(other, other_biz["id"]).status_code == 200            # other accounts have their own allowance
            # reads and decisions are not limited
            assert c.get(f"/api/businesses/{biz['id']}/proposals").status_code == 200

        # the count is in the database, so it survives a restart; a new day starts fresh
        with env(CHAT_DAILY_LIMIT="2"):
            assert ask(c, biz["id"]).status_code == 429
            with d.get_conn() as conn:
                conn.execute("UPDATE chat_usage SET day = '2000-01-01'")
            assert ask(c, biz["id"]).status_code == 200
        with env(CHAT_DAILY_LIMIT=None):
            assert config.chat_daily_limit() == 50
    finally:
        chat.run_llm = original


def test_chat_cap_is_not_spent_by_bad_requests_or_provider_outages():
    c, other = new_account(), new_account()
    biz = make_business(c)
    bid = biz["id"]
    original = chat.run_llm
    try:
        with env(CHAT_DAILY_LIMIT="1"):
            chat.run_llm = stub()
            assert c.post(f"/api/businesses/{bid}/chat", json={"messages": []}).status_code == 422
            assert ask(other, bid).status_code == 404                          # not their business
            assert ask(other, 999999).status_code == 404

            def outage(system, messages, schema):
                raise LLMError("HTTP 529")
            chat.run_llm = outage
            assert ask(c, bid).status_code == 502                              # provider down: turn is given back
            chat.run_llm = stub()
            assert ask(c, bid).status_code == 200                              # still has the one allowed turn
            assert ask(c, bid).status_code == 429
    finally:
        chat.run_llm = original


def test_existing_message_and_operation_caps_still_hold():
    c = new_account()
    bid = make_business(c)["id"]
    original = chat.run_llm
    try:
        chat.run_llm = stub()
        assert c.post(f"/api/businesses/{bid}/chat", json={"messages": [{"role": "user", "content": "x" * 4001}]}).status_code == 422
        assert c.post(f"/api/businesses/{bid}/chat", json={"messages": [{"role": "user", "content": "x"}] * 41}).status_code == 422
        many = [{"op": "create_entity_type", "label": f"T{i}"} for i in range(55)]
        chat.run_llm = stub(many)
        p = ask(c, bid).json()
        assert len(p["operations"]) == 50 and len(p["rejected_operations"]) == 5
    finally:
        chat.run_llm = original


# ---------- request body limit ----------

def test_body_size_limit():
    c = new_account()
    bid = make_business(c)["id"]
    with env(MAX_BODY_BYTES="2000"):
        big = {"name": "x" * 3000}
        r = c.post("/api/businesses", json=big)
        assert r.status_code == 413 and "too large" in r.json()["detail"]
        assert c.post("/api/businesses", json={"name": "small"}).status_code == 201

        def chunks():                                                         # no Content-Length: streamed
            for _ in range(10):
                yield b'{"name": "' + b"y" * 400 + b'"}'
        r = c.post("/api/businesses", content=chunks(), headers={"content-type": "application/json"})
        assert r.status_code == 413
        assert c.get(f"/api/businesses/{bid}").status_code == 200              # reads are unaffected
    # the default comfortably fits the largest legitimate chat request (40 messages x 4000 chars)
    original, chat.run_llm = chat.run_llm, stub()
    try:
        history = []
        for i in range(19):
            history += [{"role": "user", "content": "u" * 4000}, {"role": "assistant", "content": "a" * 4000}]
        r = ask(c, bid, "z" * 4000, history)
        assert r.status_code == 200, r.text
    finally:
        chat.run_llm = original


# ---------- healthz ----------

def test_healthz_needs_no_auth_and_returns_no_data():
    r = TestClient(app).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert "set-cookie" not in r.headers


# ---------- backup script ----------

def test_backup_is_consistent_while_the_app_writes():
    import backup_db
    src_dir = tempfile.mkdtemp()
    src = os.path.join(src_dir, "live.db")
    d.init_db(src)
    seed = sqlite3.connect(src, timeout=30)
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("INSERT INTO accounts (email, password_hash) VALUES ('seed@example.com', 'x')")
    seed.commit()
    seed.close()
    stop = threading.Event()
    written = [0]

    def keep_writing():                       # its own connection, like the app's request threads
        writer = sqlite3.connect(src, timeout=30)
        i = 0
        while not stop.is_set():
            writer.execute("INSERT INTO accounts (email, password_hash) VALUES (?, 'x')", (f"w{i}@example.com",))
            writer.commit()
            i += 1
            written[0] = i
        writer.close()

    t = threading.Thread(target=keep_writing)
    t.start()
    try:
        out = os.path.join(src_dir, "backups")
        first = backup_db.backup(src, out)
        second = backup_db.backup(src, out)
    finally:
        stop.set()
        t.join()

    assert first != second and os.path.dirname(first) == out
    assert re.fullmatch(r"operations_nerd-\d{8}-\d{6}(-\d+)?\.db", os.path.basename(first))
    for path in (first, second):
        conn = sqlite3.connect(path)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT COUNT(*) FROM accounts WHERE email='seed@example.com'").fetchone()[0] == 1
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "chat_usage" in tables
        conn.close()
    assert not [f for f in os.listdir(out) if "partial" in f or f.endswith(("-wal", "-shm"))]
    assert written[0] > 0                                                     # the writer really was running


def test_backup_errors_and_command_line():
    import backup_db
    missing = os.path.join(tempfile.mkdtemp(), "nope.db")
    try:
        backup_db.backup(missing, tempfile.mkdtemp())
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("backup of a missing database succeeded")
    assert not os.path.exists(missing)                                        # and it did not create an empty one

    live = os.path.join(tempfile.mkdtemp(), "cli.db")
    d.init_db(live)
    out = tempfile.mkdtemp()
    script = os.path.join(REPO, "scripts", "backup_db.py")
    ok = subprocess.run([sys.executable, script, "--db", live, "--out-dir", out], capture_output=True, text=True)
    assert ok.returncode == 0 and "Backup written" in ok.stdout and len(os.listdir(out)) == 1
    bad = subprocess.run([sys.executable, script, "--db", missing, "--out-dir", out], capture_output=True, text=True)
    assert bad.returncode == 1 and "Backup failed" in bad.stderr


# ---------- container files ----------

def test_dockerfile_and_dockerignore():
    docker = open(os.path.join(REPO, "Dockerfile"), encoding="utf-8").read()
    assert re.search(r"^FROM python:3\.\d+-slim", docker, re.M)
    assert re.search(r"^USER app", docker, re.M)                              # not root
    assert "requirements.txt" in docker and "pip install" in docker
    assert "DATABASE_PATH=/data/operations_nerd.db" in docker
    assert "${PORT:-8000}" in docker and "uvicorn main:app" in docker
    assert docker.index("USER app") < docker.index("CMD")
    ignore = [l.strip() for l in open(os.path.join(REPO, ".dockerignore"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    for needed in (".env", "*.db", "*-wal", "*-shm", "tests", ".git"):
        assert needed in ignore, needed
    assert "COPY . " not in docker and "COPY .env" not in docker


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
