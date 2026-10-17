"""
Deploy blockers from the storage audit:

  1. In production only /app, /api/* and /healthz are served; the legacy demo
     routes (no auth, not account-scoped) are not mounted. In local dev they are.
  2. SQLite foreign keys are enforced on every connection, and the agentic
     flows never leave orphan rows.

Each app start runs in a subprocess so it gets its own environment and a
throwaway database. Run from operations_nerd/:
    python tests/test_agentic_deploy_safety.py
"""

import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

LEGACY_GETS = ["/packs", "/packs/real_estate/questionnaire", "/businesses/1/state",
               "/businesses/1/pending-actions", "/ui/", "/ui/index.html", "/agent/"]
LEGACY_POSTS = ["/businesses", "/events", "/drafted-actions/1/approve", "/drafted-actions/1/reject"]

PROBE = r"""
import json, os
from fastapi.testclient import TestClient
import main
c = TestClient(main.app, raise_server_exceptions=False)
paths = sorted(c.get("/openapi.json").json()["paths"])
out = {"paths": paths, "get": {}, "post": {}}
for p in json.loads(os.environ["PROBE_GETS"]):
    out["get"][p] = c.get(p).status_code
for p in json.loads(os.environ["PROBE_POSTS"]):
    out["post"][p] = c.post(p, json={}).status_code
for p in ("/", "/healthz", "/app/", "/api/auth/me", "/api/businesses"):
    out["get"][p] = c.get(p).status_code
print("PROBE" + json.dumps(out))
"""


def probe(**environment) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("APP_ENV", "SESSION_SECRET", "GEMINI_API_KEY", "CONFIG_AGENT_ENABLED", "DATABASE_PATH", "LLM_PROVIDER")}
    env.update({"DATABASE_PATH": os.path.join(tempfile.mkdtemp(), "probe.db"), "RATE_LIMIT_ENABLED": "0",
                "PROBE_GETS": json.dumps(LEGACY_GETS), "PROBE_POSTS": json.dumps(LEGACY_POSTS)})
    env.update(environment)
    r = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(next(l for l in r.stdout.splitlines() if l.startswith("PROBE"))[5:])


PROD = dict(APP_ENV="production", SESSION_SECRET="s" * 40, GEMINI_API_KEY="k", LLM_PROVIDER="stub")


def test_production_does_not_mount_legacy_routes():
    out = probe(**PROD)
    for p in LEGACY_GETS:
        assert out["get"][p] == 404, (p, out["get"][p])
    for p in LEGACY_POSTS:
        assert out["post"][p] == 404, (p, out["post"][p])
    legacy_in_table = [p for p in out["paths"]
                       if p in ("/packs", "/events", "/businesses", "/ui", "/agent")
                       or p.startswith(("/packs/", "/businesses/", "/drafted-actions/", "/ui/", "/agent/"))]
    assert legacy_in_table == [], legacy_in_table


def test_production_still_serves_app_api_and_health():
    out = probe(**PROD)
    assert out["get"]["/healthz"] == 200 and out["get"]["/app/"] == 200 and out["get"]["/"] == 200
    assert out["get"]["/api/auth/me"] == 401 and out["get"]["/api/businesses"] == 401        # real routes, auth required
    assert "/api/auth/signup" in out["paths"] and "/api/businesses/{business_id}/chat" in out["paths"]


def test_local_dev_keeps_the_legacy_demo():
    out = probe(LLM_PROVIDER="stub")
    assert out["get"]["/packs"] == 200 and out["get"]["/ui/"] == 200
    assert out["post"]["/businesses"] == 422 and out["post"]["/events"] == 422          # routed, body rejected
    assert "/businesses/{business_id}/state" in out["paths"] and "/drafted-actions/{action_id}/approve" in out["paths"]
    assert out["get"]["/healthz"] == 200 and out["get"]["/app/"] == 200


# ---------- foreign keys ----------

def test_every_connection_enforces_foreign_keys():
    os.environ["LLM_PROVIDER"] = "stub"
    from db import db as d
    d.init_db(reset=True)
    with d.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        try:
            d.create_entity(conn, 999999, "crm_x", {"a": "b"})              # business 999999 does not exist
        except Exception as e:
            assert "FOREIGN KEY" in str(e).upper()
        else:
            raise AssertionError("an orphan record was accepted")


def test_agentic_flows_leave_no_orphan_rows():
    """Signup -> business -> chat -> approve -> records -> revert, then SQLite's own orphan check."""
    os.environ["LLM_PROVIDER"] = "stub"
    os.environ["SIGNUP_INVITE_CODE"] = "inv"
    os.environ["RATE_LIMIT_ENABLED"] = "0"
    from fastapi.testclient import TestClient
    from db import db as d
    d.init_db(reset=True)
    from main import app
    from agentic import chat
    from agentic.llm import LLMResult
    ops = [{"op": "create_entity_type", "label": "Partner", "ref": "lead"},
           {"op": "add_field", "entity": "lead", "label": "Name", "type": "text", "required": True}]
    chat.run_llm = lambda s, m, sc: LLMResult(json.dumps({"reply": "ok", "operations": ops}), "stub", 1, 1, 1)
    c = TestClient(app, base_url="http://testserver")
    assert c.post("/api/auth/signup", json={"email": "fk@example.com", "password": "correct horse battery",
                                            "invite_code": "inv"}).status_code == 201
    biz = c.post("/api/businesses", json={"name": "FK Biz", "template": "health_club"}).json()
    other = c.post("/api/businesses", json={"name": "Second"}).json()
    r = c.post(f"/api/businesses/{biz['id']}/chat", json={"messages": [{"role": "user", "content": "add leads"}]})
    assert r.status_code == 200, r.text
    assert c.post(f"/api/proposals/{r.json()['proposal_id']}/approve").status_code == 200
    spec = c.get(f"/api/businesses/{biz['id']}").json()["spec"]
    lead = next(e for e in spec["entities"] if e["label"] == "Partner")
    name_key = next(f["key"] for f in lead["fields"] if f["label"] == "Name")
    assert c.post(f"/api/businesses/{biz['id']}/records/{lead['key']}", json={"values": {name_key: "Ann"}}).status_code == 201
    assert c.post(f"/api/businesses/{other['id']}/records/{lead['key']}", json={"values": {}}).status_code in (404, 409)
    assert c.post(f"/api/businesses/{biz['id']}/revert", json={"to_version": 1}).status_code == 200
    with d.get_conn() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
