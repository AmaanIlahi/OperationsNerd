"""
Phase A tests for the agentic CRM: accounts, sessions, spec validation,
templates, businesses and spec versions.

Runs the same way as the other tests here (python tests/test_agentic_phase_a.py
from operations_nerd/), and is also collectable by pytest. HTTP tests go
through FastAPI's TestClient, the way a real frontend would.
"""

import copy
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from db import db as d
from main import app
from agentic import template_store
from agentic.spec import rekey_spec, empty_spec
from agentic.validator import validate_spec

os.environ["SIGNUP_INVITE_CODE"] = "let-me-in"
d.init_db(reset=True)

PASSWORD = "correct horse battery"
TEMPLATES = ["health_club", "real_estate"]


def new_client(base_url="http://testserver") -> TestClient:
    return TestClient(app, base_url=base_url)


def signup(client, email, password=PASSWORD, code="let-me-in"):
    return client.post("/api/auth/signup", json={"email": email, "password": password, "invite_code": code})


def logged_in_client(email) -> TestClient:
    client = new_client()
    assert signup(client, email).status_code == 201
    return client


def find(items, key, value):
    return next(i for i in items if i[key] == value)


# ---------- accounts ----------

def test_signup_requires_invite_code():
    c = new_client()
    assert signup(c, "a1@example.com", code=None).status_code == 403
    assert signup(c, "a1@example.com", code="wrong").status_code == 403
    assert c.get("/api/auth/me").status_code == 401   # nothing was created or logged in
    assert signup(c, "a1@example.com").status_code == 201


def test_signup_refused_when_closed_by_default():
    saved = os.environ.pop("SIGNUP_INVITE_CODE")
    os.environ.pop("ALLOW_OPEN_SIGNUP", None)
    try:
        assert signup(new_client(), "closed@example.com", code=None).status_code == 403
        assert signup(new_client(), "closed@example.com", code="let-me-in").status_code == 403
    finally:
        os.environ["SIGNUP_INVITE_CODE"] = saved


def test_signup_open_with_allow_open_signup():
    saved = os.environ.pop("SIGNUP_INVITE_CODE")
    os.environ["ALLOW_OPEN_SIGNUP"] = "1"
    try:
        assert signup(new_client(), "open@example.com", code=None).status_code == 201
    finally:
        os.environ.pop("ALLOW_OPEN_SIGNUP")
        os.environ["SIGNUP_INVITE_CODE"] = saved


def test_invite_code_still_enforced_when_open_flag_set():
    os.environ["ALLOW_OPEN_SIGNUP"] = "1"
    try:
        assert signup(new_client(), "gated@example.com", code="wrong").status_code == 403
    finally:
        os.environ.pop("ALLOW_OPEN_SIGNUP")


def test_session_secret_generated_when_unset():
    from agentic import auth
    assert not os.environ.get("SESSION_SECRET")
    assert auth._session_key() and len(auth._session_key()) >= 32


def test_signup_validation_and_duplicates():
    c = new_client()
    assert signup(c, "not-an-email").status_code == 422
    assert signup(c, "short@example.com", password="short").status_code == 422
    assert signup(c, "long@example.com", password="x" * 73).status_code == 422
    assert signup(c, "dup@example.com").status_code == 201
    assert signup(new_client(), "DUP@Example.com").status_code == 409   # email is case-insensitive


def test_login_logout_and_me():
    email = "login@example.com"
    assert signup(new_client(), email).status_code == 201

    c = new_client()
    assert c.post("/api/auth/login", json={"email": email, "password": "wrong password"}).status_code == 401
    assert c.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}).status_code == 401
    assert c.get("/api/auth/me").status_code == 401

    r = c.post("/api/auth/login", json={"email": email.upper(), "password": PASSWORD})
    assert r.status_code == 200
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == email

    old_token = c.cookies.get("session")
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    # The old token is dead server-side, not just removed from the client.
    stale = new_client()
    stale.cookies.set("session", old_token)
    assert stale.get("/api/auth/me").status_code == 401


def test_password_and_token_never_stored_in_plain_text():
    c = new_client()
    assert signup(c, "hash@example.com", password="plain text secret 123").status_code == 201
    token = c.cookies.get("session")
    with d.get_conn() as conn:
        row = conn.execute("SELECT * FROM accounts WHERE email = 'hash@example.com'").fetchone()
        sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions")]
        everything = json.dumps([dict(r) for r in conn.execute("SELECT * FROM accounts")] + sessions)
    assert "plain text secret 123" not in everything
    assert row["password_hash"].startswith("$2")          # bcrypt
    assert token not in everything                         # only a hash of the token is stored


def test_session_cookie_flags():
    local = new_client().post("/api/auth/signup", json={
        "email": "cookie-local@example.com", "password": PASSWORD, "invite_code": "let-me-in"})
    header = local.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header
    assert "secure" not in header.replace("samesite", "")   # local: plain http must still work

    prod = new_client("https://crm.example.com").post("/api/auth/signup", json={
        "email": "cookie-prod@example.com", "password": PASSWORD, "invite_code": "let-me-in"})
    header = prod.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header and "secure" in header


def test_expired_session_rejected():
    c = logged_in_client("expired@example.com")
    assert c.get("/api/auth/me").status_code == 200
    with d.get_conn() as conn:
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00+00:00'")
    assert c.get("/api/auth/me").status_code == 401


def test_business_endpoints_require_login():
    c = new_client()
    assert c.post("/api/businesses", json={"name": "X"}).status_code == 401
    assert c.get("/api/businesses").status_code == 401
    assert c.get("/api/businesses/1").status_code == 401
    assert c.get("/api/businesses/1/versions").status_code == 401


# ---------- businesses and versions ----------

def test_create_business_from_each_template():
    c = logged_in_client("tmpl@example.com")
    for template in TEMPLATES:
        r = c.post("/api/businesses", json={"name": f"My {template}", "template": template})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["current_version"] == 1 and body["template"] == template
        spec = body["spec"]
        assert spec["business"]["name"] == f"My {template}"
        assert validate_spec(spec) == []
        assert len(spec["entities"]) >= 3 and len(spec["links"]) >= 3

        # Keys are server generated, not the template's readable ones.
        template_keys = {e["key"] for e in template_store.get_template(template)["entities"]}
        assert not template_keys & {e["key"] for e in spec["entities"]}

        versions = c.get(f"/api/businesses/{body['id']}/versions").json()
        assert [v["version"] for v in versions] == [1]
        assert versions[0]["source"] == "template"
        assert c.get(f"/api/businesses/{body['id']}").json()["spec"] == spec


def test_two_businesses_from_same_template_get_different_keys():
    c = logged_in_client("rekey@example.com")
    a = c.post("/api/businesses", json={"name": "A", "template": "health_club"}).json()["spec"]
    b = c.post("/api/businesses", json={"name": "B", "template": "health_club"}).json()["spec"]
    assert {e["key"] for e in a["entities"]}.isdisjoint({e["key"] for e in b["entities"]})


def test_create_business_empty():
    c = logged_in_client("empty@example.com")
    r = c.post("/api/businesses", json={"name": "Dog Groomer"})
    assert r.status_code == 201
    body = r.json()
    assert body["template"] is None and body["current_version"] == 1
    assert body["spec"]["entities"] == [] and body["spec"]["links"] == []
    assert validate_spec(body["spec"]) == []
    assert len(c.get(f"/api/businesses/{body['id']}/versions").json()) == 1


def test_create_business_bad_input():
    c = logged_in_client("bad@example.com")
    assert c.post("/api/businesses", json={"name": "X", "template": "no_such_template"}).status_code == 422
    assert c.post("/api/businesses", json={"name": ""}).status_code == 422
    assert c.post("/api/businesses", json={"name": "   "}).status_code == 422
    assert c.get("/api/businesses").json() == []   # nothing half-created


def test_account_isolation():
    first = logged_in_client("owner1@example.com")
    second = logged_in_client("owner2@example.com")
    b1 = first.post("/api/businesses", json={"name": "First's", "template": "health_club"}).json()["id"]
    b2 = second.post("/api/businesses", json={"name": "Second's"}).json()["id"]

    assert [b["id"] for b in first.get("/api/businesses").json()] == [b1]
    assert [b["id"] for b in second.get("/api/businesses").json()] == [b2]

    assert second.get(f"/api/businesses/{b1}").status_code == 404
    assert second.get(f"/api/businesses/{b1}/versions").status_code == 404
    # Same answer as a business that does not exist at all.
    assert second.get("/api/businesses/999999").status_code == 404
    assert first.get(f"/api/businesses/{b2}").status_code == 404
    assert first.get(f"/api/businesses/{b2}/versions").status_code == 404
    assert first.get(f"/api/businesses/{b1}").status_code == 200


def test_legacy_businesses_are_not_visible():
    with d.get_conn() as conn:
        legacy_id = d.create_business(conn, "Legacy", "realestate", "1", {})
        # even one that happens to carry an owner_key but no spec
        conn.execute("UPDATE businesses SET owner_key = '1' WHERE id = ?", (legacy_id,))
    c = logged_in_client("legacy@example.com")
    assert c.get(f"/api/businesses/{legacy_id}").status_code == 404
    assert c.get("/api/businesses").json() == []


# ---------- templates ----------

def test_templates_pass_validation():
    assert sorted(template_store.list_templates()) == sorted(TEMPLATES)
    for name in TEMPLATES:
        assert validate_spec(template_store.get_template(name)) == [], name
        assert validate_spec(rekey_spec(template_store.get_template(name))) == [], name


def test_broken_template_fails_startup_load():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        spec = template_store.get_template("health_club")
        spec["entities"][0]["fields"][0]["type"] = "colour"
        with open(os.path.join(tmp, "broken.json"), "w") as f:
            json.dump(spec, f)
        try:
            template_store._load_all(tmp)
        except template_store.TemplateError as e:
            assert "broken.json" in str(e)
        else:
            raise AssertionError("broken template was accepted")


# ---------- validator ----------

def base_spec():
    return {
        "business": {"name": "Test", "industry": None},
        "entities": [
            {"key": "crm_member", "label": "Member", "archived": False, "fields": [
                {"key": "f_name", "label": "Name", "type": "text", "required": True, "archived": False},
                {"key": "f_plan", "label": "Plan", "type": "select", "options": ["A", "B"],
                 "required": False, "archived": False}]},
            {"key": "crm_location", "label": "Location", "archived": False, "fields": []},
        ],
        "links": [
            {"key": "l_home", "label": "Home", "from": "crm_member", "to": "crm_location",
             "cardinality": "many_to_one", "archived": False}],
    }


def mutated(fn):
    spec = copy.deepcopy(base_spec())
    fn(spec)
    return validate_spec(spec)


def assert_rejected(fn, fragment):
    issues = mutated(fn)
    assert issues, f"expected an issue containing '{fragment}', got none"
    assert any(fragment in i for i in issues), f"no issue contains '{fragment}': {issues}"


def test_validator_accepts_base_spec_and_empty_spec():
    assert validate_spec(base_spec()) == []
    assert validate_spec(empty_spec("Empty")) == []


def test_validator_rejects_bad_entity_key():
    assert_rejected(lambda s: s["entities"][0].update(key="member"), "must be crm_")
    assert_rejected(lambda s: s["entities"][0].update(key="contact"), "must be crm_")
    assert_rejected(lambda s: s["entities"][0].update(key="crm_Member!"), "must be crm_")


def test_validator_rejects_bad_field_and_link_keys():
    assert_rejected(lambda s: s["entities"][0]["fields"][0].update(key="name"), "must be f_")
    assert_rejected(lambda s: s["links"][0].update(key="home"), "must be l_")


def test_validator_rejects_duplicate_keys():
    assert_rejected(lambda s: s["entities"][1].update(key="crm_member"), "already used")
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(key="f_name"), "already used")
    assert_rejected(lambda s: s["links"][0].update(key="f_name"), "already used")


def test_validator_rejects_duplicate_entity_labels():
    assert_rejected(lambda s: s["entities"][1].update(label="  member "), "duplicate entity label")


def test_validator_rejects_duplicate_field_labels():
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(label="NAME"), "duplicate field label")

    def archived_dup(s):
        s["entities"][0]["fields"][1].update(label="Name", type="text", options=None, archived=True)
    assert_rejected(archived_dup, "duplicate field label")   # restoring it would collide


def test_same_field_label_on_different_entities_is_fine():
    def add(s):
        s["entities"][1]["fields"].append(
            {"key": "f_loc_name", "label": "Name", "type": "text", "required": False, "archived": False})
    assert mutated(add) == []


def test_validator_rejects_unsupported_field_type():
    assert_rejected(lambda s: s["entities"][0]["fields"][0].update(type="colour"), "type")


def test_validator_rejects_select_without_options():
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(options=[]), "non-empty options")
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(options=None), "non-empty options")

    def multiselect_none(s):
        s["entities"][0]["fields"][1].update(type="multiselect", options=None)
    assert_rejected(multiselect_none, "non-empty options")


def test_validator_rejects_bad_options():
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(options=["A", " "]), "must not be blank")
    assert_rejected(lambda s: s["entities"][0]["fields"][1].update(options=["A", "a"]), "must be unique")
    assert_rejected(lambda s: s["entities"][0]["fields"][0].update(options=["x"]), "only allowed on select")


def test_validator_rejects_link_to_missing_entity():
    assert_rejected(lambda s: s["links"][0].update(to="crm_ghost"), "does not exist")
    assert_rejected(lambda s: s["links"][0].update(**{"from": "crm_ghost"}), "does not exist")


def test_validator_rejects_active_link_to_archived_entity():
    assert_rejected(lambda s: s["entities"][1].update(archived=True), "archived entity")

    def archived_both(s):
        s["entities"][1]["archived"] = True
        s["links"][0]["archived"] = True
    assert mutated(archived_both) == []      # archiving the link along with it is consistent


def test_validator_rejects_malformed_specs():
    assert validate_spec([]) != []
    assert validate_spec({}) != []
    assert_rejected(lambda s: s.update(surprise=1), "surprise")
    assert_rejected(lambda s: s["entities"][0].update(colour="red"), "colour")
    assert_rejected(lambda s: s["links"][0].update(cardinality="some_to_some"), "cardinality")
    assert_rejected(lambda s: s["entities"][0].update(label=" "), "label is empty")
    assert_rejected(lambda s: s["business"].update(name=""), "label is empty")


def test_validator_reports_every_problem():
    def many(s):
        s["entities"][0].update(key="member")
        s["links"][0].update(to="crm_ghost")
        s["entities"][0]["fields"][1].update(options=[])
    assert len(mutated(many)) >= 3


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
