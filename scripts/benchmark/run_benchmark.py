"""
Benchmark harness for the agentic CRM (Phase F).

Runs the task suite in tasks.yaml against the app's real HTTP API (FastAPI
TestClient, in process) and the real model, on a throwaway SQLite database.
One account per business; every task runs --runs times (a fresh business per
run, tasks in order inside it). Per task it records pass/fail with reasons,
owner actions, time, tokens and cost (tokens from the proposals audit row),
operations proposed / applied / refused / retries, record-level change
impact, and spec versions. Around the whole run it records `git diff` on
core code (the "code leak" measure; it must be empty).

    python scripts/benchmark/run_benchmark.py --runs 3
    python scripts/benchmark/run_benchmark.py --dry-run --runs 1     # scripted stub, no network

Output: scripts/benchmark/results/<timestamp>/results.json and report.md.

The harness never changes agent, validator or app behaviour: a task that
fails is reported, not worked around. Core code is imported, never edited.
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

import yaml

import history

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
APP_DIR = os.path.join(REPO, "operations_nerd")
TASKS_FILE = os.path.join(HERE, "tasks.yaml")
PASSWORD = "benchmark-password-1"
INVITE = "benchmark-invite"

CATEGORIES = ("entities_added", "entities_changed", "entities_removed",
              "fields_added", "fields_changed", "fields_removed",
              "links_added", "links_changed", "links_removed")


# =====================================================================
# spec helpers
# =====================================================================

def active(items):
    return [i for i in items if not i.get("archived")]


def matches(label: str, alias: str) -> bool:
    label, alias = label.lower(), alias.lower()
    return label == alias or alias in label


def spec_diff(a: dict, b: dict) -> dict:
    """What changed between two specs, by kind. Keys are permanent, so
    everything is matched on keys, never labels."""
    out = {c: [] for c in CATEGORIES}
    ea, eb = {e["key"]: e for e in a["entities"]}, {e["key"]: e for e in b["entities"]}
    for k in eb:
        if k not in ea:
            out["entities_added"].append(k)
    for k in ea:
        if k not in eb:
            out["entities_removed"].append(k)
            continue
        if (ea[k]["label"], ea[k]["archived"]) != (eb[k]["label"], eb[k]["archived"]):
            out["entities_changed"].append(k)
        fa, fb = {f["key"]: f for f in ea[k]["fields"]}, {f["key"]: f for f in eb[k]["fields"]}
        out["fields_added"] += [(k, f) for f in fb if f not in fa]
        out["fields_removed"] += [(k, f) for f in fa if f not in fb]
        out["fields_changed"] += [(k, f) for f in fa if f in fb and fa[f] != fb[f]]
    la, lb = {l["key"]: l for l in a["links"]}, {l["key"]: l for l in b["links"]}
    out["links_added"] = [k for k in lb if k not in la]
    out["links_removed"] = [k for k in la if k not in lb]
    out["links_changed"] = [k for k in la if k in lb and la[k] != lb[k]]
    return out


def find_entity(spec, key):
    return next((e for e in spec["entities"] if e["key"] == key), None)


def find_field(spec, ekey, fkey):
    e = find_entity(spec, ekey)
    return next((f for f in e["fields"] if f["key"] == fkey), None) if e else None


def primary_entity(spec, biz):
    live = [e for e in active(spec["entities"]) if active(e["fields"])]
    for alias in biz["primary"]:
        for e in live:
            if matches(e["label"], alias):
                return e
    return live[0] if live else None


# ---- selectors: choose the concrete thing a task is about, from the spec the
# ---- previous tasks left behind. None means "no suitable target".

def sel_primary_entity(spec, biz):
    e = primary_entity(spec, biz)
    return {"entity": e["label"], "entity_key": e["key"]} if e else None


def _non_first_fields(e):
    return active(e["fields"])[1:]


def sel_retypable_field(spec, biz):
    e = primary_entity(spec, biz)
    if not e:
        return None
    fields = _non_first_fields(e)
    for ftype, new_type, phrase in (("text", "long_text", "long text"), ("phone", "text", "text"),
                                    ("email", "text", "text")):
        for f in fields:
            if f["type"] == ftype:
                return {"entity": e["label"], "entity_key": e["key"], "field": f["label"],
                        "field_key": f["key"], "new_type": new_type, "new_type_phrase": phrase}
    return None


def sel_renamable_field(spec, biz):
    e = primary_entity(spec, biz)
    if not e:
        return None
    labels = {f["label"].lower() for f in e["fields"]}
    for f in _non_first_fields(e):
        new_label = f"Preferred {f['label']}"
        if new_label.lower() not in labels:
            return {"entity": e["label"], "entity_key": e["key"], "field": f["label"],
                    "field_key": f["key"], "new_label": new_label}
    return None


def _removable(spec, biz, from_end):
    e = primary_entity(spec, biz)
    if not e:
        return None
    fields = [f for f in _non_first_fields(e) if f["type"] not in ("select", "multiselect")] or _non_first_fields(e)
    if not fields:
        return None
    f = fields[-from_end] if len(fields) >= from_end else fields[-1]
    return {"entity": e["label"], "entity_key": e["key"], "field": f["label"], "field_key": f["key"]}


def sel_removable_field_a(spec, biz):
    return _removable(spec, biz, 1)


def sel_removable_field_b(spec, biz):
    return _removable(spec, biz, 2)


def sel_unlinked_pair(spec, biz):
    live = active(spec["entities"])
    p = primary_entity(spec, biz)
    linked = {frozenset((l["from"], l["to"])) for l in active(spec["links"])}
    order = ([p] if p else []) + [e for e in live if not p or e["key"] != p["key"]]
    for a in order:
        for b in live:
            if a["key"] != b["key"] and frozenset((a["key"], b["key"])) not in linked:
                return {"entity_a": a["label"], "entity_a_key": a["key"],
                        "entity_b": b["label"], "entity_b_key": b["key"]}
    return None


SELECTORS = {f[4:]: fn for f, fn in globals().items() if f.startswith("sel_")}


# =====================================================================
# record hashing (change impact)
# =====================================================================

def record_hashes(conn) -> dict:
    """{(business_id, record_id): sha256 of the whole record, timestamps included}."""
    attrs = {}
    for r in conn.execute(
            """SELECT a.entity_id, a.attribute_name, a.value FROM entity_attributes a
               JOIN entities e ON e.id = a.entity_id AND e.entity_type = a.entity_type"""):
        attrs.setdefault(r["entity_id"], []).append((r["attribute_name"], r["value"]))
    out = {}
    for r in conn.execute("SELECT id, business_id, entity_type, created_at, updated_at FROM entities"):
        payload = json.dumps([r["id"], r["business_id"], r["entity_type"], r["created_at"], r["updated_at"],
                              sorted(attrs.get(r["id"], []))])
        out[(r["business_id"], r["id"])] = hashlib.sha256(payload.encode()).hexdigest()
    return out


def impact(before: dict, after: dict, business_id: int) -> dict:
    own = lambda h: {k: v for k, v in h.items() if k[0] == business_id}
    other = lambda h: {k: v for k, v in h.items() if k[0] != business_id}
    b, a = own(before), own(after)
    return {
        "records_before": len(b),
        "changed": sorted(k[1] for k in b if k in a and a[k] != b[k]),
        "lost": sorted(k[1] for k in b if k not in a),
        "added": sorted(k[1] for k in a if k not in b),
        "other_businesses_identical": other(before) == other(after),
    }


# =====================================================================
# git "code leak"
# =====================================================================

def git_state() -> dict:
    """Core code = everything outside scripts/benchmark/ and database files."""
    spec = ["--", ".", ":(exclude)scripts/benchmark", ":(exclude)*.db", ":(exclude)*.db-wal", ":(exclude)*.db-shm"]

    def git(*args):
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else f"(git failed: {r.stderr.strip()[:200]})"
    lines = 0
    for row in git("diff", "--numstat", "HEAD", *spec).splitlines():
        added, deleted, *_ = row.split("\t")
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    for path in git("ls-files", "--others", "--exclude-standard", *spec).splitlines():
        try:
            with open(os.path.join(REPO, path), encoding="utf-8", errors="replace") as f:
                lines += sum(1 for _ in f)
        except OSError:
            pass
    return {"diff_stat": git("diff", "--stat", "HEAD", *spec),
            "status": git("status", "--porcelain", *spec),
            "head": git("rev-parse", "--short", "HEAD"),
            "lines": lines}


def tree_state() -> dict:
    """Where the run happened: commit, branch, and whether the working tree
    had uncommitted changes anywhere (benchmark output under results/ does not
    count, since every run creates it)."""
    def git(*args):
        r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    files = [l for l in git("status", "--porcelain", "--", ".", ":(exclude)scripts/benchmark/results").splitlines() if l]
    return {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(files), "dirty_files": files}


def system_prompt_hash() -> str:
    """sha256 of the agent's system prompt as built for an empty business, so
    any wording or operation-shape change shows up as a different hash."""
    sys.path.insert(0, APP_DIR)
    from agentic import chat
    from agentic.spec import empty_spec
    return hashlib.sha256(chat.build_system_prompt(empty_spec("x"), None, []).encode("utf-8")).hexdigest()


def price_meta(config, args) -> dict:
    supplied = args.price_in is not None and args.price_out is not None
    return {**config["pricing"], "basis": "supplied" if supplied else config["pricing"].get("basis", "assumed")}


# =====================================================================
# app + client plumbing
# =====================================================================

class Harness:
    def __init__(self, args, config):
        self.args, self.config = args, config
        self.llm_calls = 0

    # -- setup: environment first, then the app is imported --
    def setup_app(self):
        self.tmp = tempfile.mkdtemp(prefix="agentic_bench_")
        os.environ.update({
            "DATABASE_PATH": os.path.join(self.tmp, "benchmark.db"),
            "RATE_LIMIT_ENABLED": "0", "SIGNUP_INVITE_CODE": INVITE,
            "CHAT_DAILY_LIMIT": "100000", "SESSION_SECRET": "benchmark-secret-" + "x" * 32,
        })
        os.environ.pop("CONFIG_AGENT_ENABLED", None)
        os.environ["LLM_PROVIDER"] = "stub" if self.args.dry_run else (self.args.provider or "gemini")
        if self.args.model and not self.args.dry_run:
            os.environ["GEMINI_MODEL" if os.environ["LLM_PROVIDER"] == "gemini" else "ANTHROPIC_MODEL"] = self.args.model
        os.chdir(APP_DIR)                            # main.py mounts ./frontend relative to here
        sys.path.insert(0, APP_DIR)
        sys.path.insert(0, HERE)
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
        from fastapi.testclient import TestClient
        from agentic import chat, llm
        from db import db as d
        import main as app_main
        self.d, self.chat, self.llm, self.TestClient, self.app = d, chat, llm, TestClient, app_main.app
        assert os.path.abspath(d.DB_PATH) == os.path.abspath(os.environ["DATABASE_PATH"]), "not on the throwaway database"
        if self.args.dry_run:
            import dry_run
            inner = dry_run.DryRunLLM()
        else:
            inner = chat.run_llm
        self.model_name = None

        def counting(system, messages, schema):
            self.llm_calls += 1
            return inner(system, messages, schema)
        chat.run_llm = counting                      # counts calls only; behaviour is the app's own
        if not self.args.dry_run:
            key_env = llm.required_key_env()
            if key_env and not os.environ.get(key_env):
                sys.exit(f"{key_env} is not set (needed for LLM_PROVIDER={llm.get_provider()}). Use --dry-run to test without a model.")

    def client(self):
        return self.TestClient(self.app, base_url="http://testserver", raise_server_exceptions=False)

    def signup(self, email):
        c = self.client()
        r = c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "invite_code": INVITE})
        assert r.status_code == 201, r.text
        return c

    def business(self, c, bid):
        r = c.get(f"/api/businesses/{bid}")
        assert r.status_code == 200, r.text
        return r.json()

    def hashes(self):
        with self.d.get_conn() as conn:
            return record_hashes(conn)

    def audit(self, proposal_id):
        with self.d.get_conn() as conn:
            row = conn.execute("SELECT model, input_tokens, output_tokens, latency_ms FROM proposals WHERE id = ?",
                               (proposal_id,)).fetchone()
        return dict(row) if row else None

    def record_count(self, business_id, entity_key):
        with self.d.get_conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM entities WHERE business_id = ? AND entity_type = ?",
                                (business_id, entity_key)).fetchone()[0]


# =====================================================================
# seeding (after T01)
# =====================================================================

def seed_value(field, i, entity_label):
    t = field["type"]
    opts = field.get("options") or []
    return {"text": f"Seed {entity_label} {i}", "long_text": f"Seed note {i}", "number": i * 10,
            "date": f"2026-01-0{i}", "boolean": i % 2 == 0,
            "select": opts[(i - 1) % len(opts)] if opts else None,
            "multiselect": opts[:1], "email": f"seed{i}@example.com", "phone": f"555-010{i}"}[t]


def seed_records(h, c, business_id, biz, cfg):
    spec = h.business(c, business_id)["spec"]
    chosen = []
    for alias in biz["seed_entities"]:
        for e in active(spec["entities"]):
            if matches(e["label"], alias) and e not in chosen and active(e["fields"]):
                chosen.append(e)
    chosen = (chosen or [e for e in active(spec["entities"]) if active(e["fields"])])[:cfg["max_entities"]]
    created, errors = 0, []
    for e in chosen:
        for i in range(1, cfg["records_per_entity"] + 1):
            values = {f["key"]: seed_value(f, i, e["label"]) for f in active(e["fields"])}
            values = {k: v for k, v in values.items() if v is not None}
            r = c.post(f"/api/businesses/{business_id}/records/{e['key']}", json={"values": values})
            if r.status_code == 201:
                created += 1
            else:
                errors.append({"entity": e["label"], "status": r.status_code, "detail": str(r.text)[:200]})
    return {"entities": [e["label"] for e in chosen], "created": created, "errors": errors}


# =====================================================================
# checks
# =====================================================================

class Ctx:
    """Everything a check may look at for one task run."""
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def after_spec(self):
        return self.snapshots[-1]

    def spec_at(self, step):
        return self.snapshots[(step or len(self.snapshots)) - 1]


def fmt(params: dict, variables: dict) -> dict:
    return {k: (v.format_map(variables) if isinstance(v, str) else v) for k, v in params.items()}


def ck_entities_present(c, p):
    missing = [" / ".join(g) for g in c.biz["expect_entities"]
               if not any(matches(e["label"], a) for e in active(c.after_spec["entities"]) for a in g)]
    return (not missing, "no entity type matching: " + "; ".join(missing))


def ck_entities_have_fields(c, p):
    bare = [e["label"] for e in active(c.after_spec["entities"]) if not active(e["fields"])]
    return (not bare, "entity types without fields: " + ", ".join(bare))


def ck_field_added(c, p):
    diff = spec_diff(c.before_spec, c.after_spec)
    wanted = [o.lower() for o in p.get("options_include", [])]
    for ekey, fkey in diff["fields_added"]:
        f = find_field(c.after_spec, ekey, fkey)
        if ekey != p["entity"] or p["label_contains"].lower() not in f["label"].lower():
            continue
        if f["type"] != p["field_type"]:
            return False, f"field '{f['label']}' was added with type {f['type']}, expected {p['field_type']}"
        have = [o.lower() for o in f.get("options") or []]
        gone = [o for o in wanted if o not in have]
        return (not gone, f"field '{f['label']}' lacks options: {gone}")
    return False, f"no new field containing '{p['label_contains']}' on the expected entity"


def ck_only_changes(c, p):
    diff = spec_diff(c.before_spec, c.after_spec)
    extra = {k: v for k, v in diff.items() if v and k not in p["allow"]}
    return (not extra, "unexpected spec changes: " + json.dumps(extra, default=str)[:300])


def ck_version_increased(c, p):
    return (c.version_after > c.version_before, f"version stayed at {c.version_before}")


def ck_version_unchanged(c, p):
    return (c.version_after == c.version_before, f"version moved {c.version_before} -> {c.version_after}")


def ck_field_type_is(c, p):
    f = find_field(c.after_spec, p["entity"], p["field"])
    return (f is not None and f["type"] == p["field_type"],
            "field missing" if f is None else f"type is {f['type']}, expected {p['field_type']}")


def ck_field_renamed(c, p):
    f = find_field(c.after_spec, p["entity"], p["field"])
    ok = f is not None and f["label"].strip().lower() == p["new_label"].strip().lower()
    return ok, "field missing under its old key" if f is None else f"label is '{f['label']}', expected '{p['new_label']}'"


def ck_entity_added(c, p):
    diff = spec_diff(c.before_spec, c.after_spec)
    for k in diff["entities_added"]:
        e = find_entity(c.after_spec, k)
        if p["label_contains"].lower() in e["label"].lower():
            n = len(active(e["fields"]))
            return (n >= p.get("min_fields", 0), f"new entity '{e['label']}' has {n} fields")
    return False, f"no new entity type containing '{p['label_contains']}'"


def _resolve_entity(spec, key=None, label_contains=None):
    if key:
        return find_entity(spec, key)
    return next((e for e in spec["entities"] if label_contains.lower() in e["label"].lower()), None)


def ck_link_between(c, p):
    spec = c.after_spec
    a = _resolve_entity(spec, p.get("a"), p.get("a_label_contains"))
    b = _resolve_entity(spec, p.get("b"), p.get("b_label_contains"))
    if not a or not b:
        return False, "one end of the expected link does not exist"
    ends = {a["key"], b["key"]}
    ok = any({l["from"], l["to"]} == ends and not l["archived"] for l in spec["links"])
    return ok, f"no active link between '{a['label']}' and '{b['label']}'"


def ck_field_archived(c, p):
    f = find_field(c.spec_at(p.get("after_step")), p["entity"], p["field"])
    return (f is not None and f["archived"], "field not archived" if f else "field gone entirely")


def ck_field_active(c, p):
    f = find_field(c.spec_at(p.get("after_step")), p["entity"], p["field"])
    return (f is not None and not f["archived"], "field still archived" if f else "field gone entirely")


def ck_spec_equals_task_start(c, p):
    return (c.after_spec == c.before_spec, "spec differs from the spec at task start: "
            + json.dumps({k: v for k, v in spec_diff(c.before_spec, c.after_spec).items() if v}, default=str)[:300])


def ck_spec_unchanged(c, p):
    return ck_spec_equals_task_start(c, p)


def ck_duplicate_not_proposed(c, p):
    ops = c.steps[(p.get("after_step") or len(c.steps)) - 1].get("applied_ops", [])
    dup = [o for o in ops if o.get("op") == "create_entity_type" and o.get("label", "").lower() == p["label"].lower()]
    return (not dup, "the duplicate entity type was proposed as valid")


def ck_needs_approval(c, p):
    s = c.steps[0]
    return (s.get("version_after_chat") == c.version_before,
            "the spec changed before the owner decided anything")


def ck_has_records(c, p):
    n = c.records_in(p["entity"])
    return (n > 0, f"the entity has {n} records, so this task does not test what it should")


CHECKS = {f[3:]: fn for f, fn in globals().items() if f.startswith("ck_")}


# =====================================================================
# one task run
# =====================================================================

def post_json(c, path, body=None):
    r = c.post(path, json=body) if body is not None else c.post(path)
    try:
        data = r.json()
    except ValueError:
        data = {"detail": r.text[:200]}
    return r.status_code, data


def run_task(h, task, biz, c, business_id, price):
    started = time.monotonic()
    calls_before = h.llm_calls
    biz_before = h.business(c, business_id)
    spec_before, version_before = biz_before["spec"], biz_before["current_version"]
    hashes_before = h.hashes()
    result = {"task": task["id"], "title": task["title"], "steps": [], "checks": [], "errors": []}
    variables = {"describe": " ".join(biz["describe"].split())}

    if task.get("bind"):
        bound = SELECTORS[task["bind"]](spec_before, biz)
        if bound is None:
            result.update(passed=False, precondition_failed=True, seconds=0.0)
            result["checks"].append({"type": "precondition", "ok": False,
                                     "reason": f"no suitable target for '{task['bind']}' (earlier tasks left no usable entity or field)"})
            result.update(_empty_metrics(version_before))
            return result
        variables.update(bound)
        result["bound"] = bound

    snapshots, proposal_ids, owner_actions = [], [], 0
    for step in task["steps"]:
        s = {}
        if "say" in step:
            msg = step["say"].format_map(variables)
            s.update(message=msg)
            owner_actions += 1
            n0 = h.llm_calls
            code, data = post_json(c, f"/api/businesses/{business_id}/chat",
                                   {"messages": [{"role": "user", "content": msg}]})
            s.update(http=code, llm_calls=h.llm_calls - n0)
            if code != 200:
                s.update(error=str(data.get("detail", data))[:300])
            else:
                pid = data["proposal_id"]
                proposal_ids.append(pid)
                s.update(proposal_id=pid, status=data["status"], reply=data["reply"][:300],
                         applied_ops=[o["op"] for o in data["operations"]],
                         refused=[{"op": r["op"].get("op"), "reasons": r["reasons"]} for r in data["rejected_operations"]],
                         impact=data["impact"], retries=max(0, s["llm_calls"] - 1))
                s["version_after_chat"] = h.business(c, business_id)["current_version"]
                if data["status"] == "pending" and step.get("decision") in ("approve", "reject"):
                    owner_actions += 1
                    dcode, ddata = post_json(c, f"/api/proposals/{pid}/{step['decision']}")
                    s.update(decision=step["decision"], decision_http=dcode)
                    if dcode == 200 and step["decision"] == "approve":
                        s["ops_applied"] = len(s["applied_ops"])
                    if dcode != 200:
                        s["error"] = f"{step['decision']} failed: {str(ddata.get('detail', ddata))[:300]}"
                else:
                    s["decision"] = "none (nothing pending)" if data["status"] != "pending" else "none"
        elif step.get("action") == "revert_to_task_start":
            owner_actions += 1
            code, data = post_json(c, f"/api/businesses/{business_id}/revert", {"to_version": version_before})
            s.update(action="revert", http=code)
            if code != 200:
                s["error"] = f"revert failed: {str(data.get('detail', data))[:300]}"
        result["steps"].append(s)
        if s.get("error"):
            result["errors"].append(s["error"])
        snapshots.append(h.business(c, business_id)["spec"])

    after = h.business(c, business_id)
    seconds = time.monotonic() - started
    hashes_after = h.hashes()
    imp = impact(hashes_before, hashes_after, business_id)

    audits = [a for a in (h.audit(p) for p in proposal_ids) if a]
    tin = sum(a["input_tokens"] or 0 for a in audits)
    tout = sum(a["output_tokens"] or 0 for a in audits)
    ops_valid = sum(len(s.get("applied_ops", [])) for s in result["steps"])      # passed the validator
    ops_applied = sum(s.get("ops_applied", 0) for s in result["steps"])          # approved and written
    ops_refused = sum(len(s.get("refused", [])) for s in result["steps"])
    ctx = Ctx(biz=biz, before_spec=spec_before, snapshots=snapshots, steps=result["steps"],
              version_before=version_before, version_after=after["current_version"],
              records_in=lambda ekey: _count_at_start(h, business_id, ekey, hashes_before))

    ok_all = not result["errors"]
    if result["errors"]:
        result["checks"].append({"type": "steps_completed", "ok": False, "reason": "; ".join(result["errors"])})
    for chk in task["checks"]:
        p = fmt({k: v for k, v in chk.items() if k != "type"}, variables)
        try:
            ok, reason = CHECKS[chk["type"]](ctx, p)
        except Exception as e:                       # a check that cannot run is a failure, with the reason
            ok, reason = False, f"check could not run: {type(e).__name__}: {e}"
        result["checks"].append({"type": chk["type"], "ok": bool(ok), "reason": None if ok else reason})
        ok_all &= bool(ok)
    unintended = len(imp["changed"]) + len(imp["lost"]) + len(imp["added"])
    for name, ok, reason in (
            ("no_unintended_record_changes", unintended == 0,
             f"records changed={imp['changed']} lost={imp['lost']} added={imp['added']}"),
            ("other_businesses_identical", imp["other_businesses_identical"],
             "another business's records changed")):
        result["checks"].append({"type": name, "ok": ok, "reason": None if ok else reason})
        ok_all &= ok

    result.update(
        passed=ok_all, seconds=round(seconds, 2), owner_actions=owner_actions,
        input_tokens=tin, output_tokens=tout,
        cost_usd=round(tin * price["input_per_mtok"] / 1e6 + tout * price["output_per_mtok"] / 1e6, 6),
        audit_latency_ms=sum(a["latency_ms"] or 0 for a in audits),
        llm_calls=h.llm_calls - calls_before,
        ops_proposed=ops_valid + ops_refused, ops_valid=ops_valid, ops_applied=ops_applied, ops_refused=ops_refused,
        retries=sum(s.get("retries", 0) for s in result["steps"]),
        impact=imp, unintended_record_changes=unintended,
        version_before=version_before, version_after=after["current_version"],
        model=(audits[0]["model"] if audits else None),
    )
    return result


def _empty_metrics(version):
    return dict(owner_actions=0, input_tokens=0, output_tokens=0, cost_usd=0.0, audit_latency_ms=0, llm_calls=0,
                ops_proposed=0, ops_valid=0, ops_applied=0, ops_refused=0, retries=0, unintended_record_changes=0,
                impact={"records_before": 0, "changed": [], "lost": [], "added": [], "other_businesses_identical": True},
                version_before=version, version_after=version, model=None)


def _count_at_start(h, business_id, ekey, hashes_before):
    """Records of this entity type that existed when the task started."""
    with h.d.get_conn() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM entities WHERE business_id = ? AND entity_type = ?",
                                          (business_id, ekey))]
    return sum(1 for i in ids if (business_id, i) in hashes_before)


# =====================================================================
# failure classification
# =====================================================================

BUG, GAP, BREAK = "implementation bug", "pack-authoring gap", "true break"


def classify(t: dict) -> tuple[str, str]:
    """A first-pass label with the reasoning; the report asks for human review."""
    failed = [c for c in t["checks"] if not c["ok"]]
    steps = t["steps"]
    errs = " ".join(t["errors"])
    if t.get("precondition_failed"):
        return GAP, "Cascade: an earlier task left nothing this task could act on. The agent did not build the structure it was asked for (see T01)."
    if any(s.get("http", 200) >= 500 and s.get("http") != 502 for s in steps) or any("failed:" in e for e in t["errors"]):
        return BUG, "The server returned an unexpected error status (or approve/reject/revert was refused)."
    if any(c["type"] in ("no_unintended_record_changes", "other_businesses_identical") for c in failed):
        return BUG, "Records changed (or another business was affected) though the task should never touch data. Spec operations must not rewrite records."
    if "unreadable" in errs or "unexpected answer" in errs:
        return BUG, "The provider integration returned output the app could not parse, even after the one retry (structured output not honoured)."
    if "unavailable" in errs or any(s.get("http") == 502 for s in steps):
        return BUG, "The model provider call failed (502). Often transient (rate limit or outage); rerun before treating it as a defect."
    types = {c["type"] for c in failed}
    if types & {"spec_unchanged", "version_unchanged"} and t["task"] in ("T09", "T10"):
        return BUG, "The spec changed although the harness rejected every proposal: a rejected or never-approved proposal must change nothing."
    if "spec_equals_task_start" in types and t["task"] in ("T07", "T08"):
        decided = [s for s in steps if s.get("applied_ops") or s.get("action")]
        if len(decided) == len(steps) and not any(s.get("refused") for s in steps):
            return BUG, "Remove then restore/revert was applied without refusals but did not return to the original spec: the round trip is lossy."
        return GAP, "The agent did not produce the right restore operation, so the spec never returned to its original state; the operation set can express it."
    if t["task"] != "T09":                       # T09 expects refusals; a refusal there is the system working
        for st in steps:
            n_refused, n_valid = len(st.get("refused", [])), len(st.get("applied_ops", []))
            if n_refused and not st.get("retries"):
                return BUG, (f"{n_refused} of {n_refused + n_valid} operations were refused and the turn was not retried: "
                             "any refusal must trigger one retry with the reasons.")
            if n_refused and st.get("retries") and n_refused > n_valid:
                return BUG, (f"Still mostly refused after the retry ({n_refused} of {n_refused + n_valid} operations): "
                             "the retry did not repair the proposal.")
    refused = [r for s in steps for r in s.get("refused", [])]
    if refused:
        reasons = "; ".join(sorted({x for r in refused for x in r["reasons"]}))[:240]
        return GAP, f"The agent's operations were refused by the validator (even after the retry): {reasons}. The prompt or output schema did not steer it to valid operations; the request itself is expressible."
    proposed = sum(len(s.get("applied_ops", [])) for s in steps)
    if proposed == 0:
        return GAP, "The agent proposed no operations. The request is expressible with the existing operation set, so this is a prompt/context gap."
    return GAP, "The agent proposed valid operations that did not match the intent (" + "; ".join(
        f"{c['type']}: {c['reason']}" for c in failed)[:240] + "). The request is expressible; the guidance was not specific enough."


# =====================================================================
# orchestration
# =====================================================================

def median(xs):
    return statistics.median(xs) if xs else 0


def run_all(h, config, args):
    tasks = [t for t in config["tasks"] if not args.tasks or t["id"] in args.tasks]
    businesses = [b for b in config["businesses"] if not args.businesses or b["id"] in args.businesses]
    price = config["pricing"]
    out = []
    for biz in businesses:
        c = h.signup(f"bench-{biz['id']}@example.com")
        entry = {"business": biz["id"], "name": biz["name"], "template": biz["template"], "runs": []}
        for run in range(1, args.runs + 1):
            r = c.post("/api/businesses", json={"name": f"{biz['name']} (run {run})", "template": biz["template"]})
            assert r.status_code == 201, r.text
            bid = r.json()["id"]
            run_entry = {"run": run, "business_id": bid, "tasks": [], "seed": None}
            root = None                     # the first failure in this run; later "no target" failures inherit it
            for task in tasks:
                t = run_task(h, task, biz, c, bid, price)
                if t["passed"]:
                    t["classification"] = None
                else:
                    t["classification"], t["classification_reason"] = classify(t)
                    if t.get("precondition_failed") and root:
                        t["classification"] = root[0]
                        t["classification_reason"] = f"Cascade from {root[2]}: {root[1]}"
                    elif root is None:
                        root = (t["classification"], t["classification_reason"], task["id"])
                run_entry["tasks"].append(t)
                print(f"[{biz['id']} run {run}] {task['id']} {'PASS' if t['passed'] else 'FAIL'} "
                      f"{t['seconds']}s {t['input_tokens'] + t['output_tokens']} tok", flush=True)
                if task["id"] == "T01":
                    run_entry["seed"] = seed_records(h, c, bid, biz, config["seed"])
                    print(f"    seeded {run_entry['seed']['created']} records in {run_entry['seed']['entities']}", flush=True)
            entry["runs"].append(run_entry)
        out.append(entry)
    return out


def summarize(results, config, args):
    price = config["pricing"]
    rows = []
    task_ids = [t["id"] for t in config["tasks"] if not args.tasks or t["id"] in args.tasks]
    for tid in task_ids:
        for b in results:
            ts = [t for run in b["runs"] for t in run["tasks"] if t["task"] == tid]
            if not ts:
                continue
            rows.append({
                "task": tid, "business": b["business"], "runs": len(ts), "passed": sum(t["passed"] for t in ts),
                "median_seconds": round(median([t["seconds"] for t in ts]), 1),
                "median_tokens": int(median([t["input_tokens"] + t["output_tokens"] for t in ts])),
                "median_cost_usd": round(median([t["cost_usd"] for t in ts]), 5),
                "median_owner_actions": median([t["owner_actions"] for t in ts]),
                "unintended_record_changes": sum(t["unintended_record_changes"] for t in ts),
                "ops_proposed": sum(t["ops_proposed"] for t in ts), "ops_valid": sum(t["ops_valid"] for t in ts),
                "ops_applied": sum(t["ops_applied"] for t in ts),
                "ops_refused": sum(t["ops_refused"] for t in ts), "retries": sum(t["retries"] for t in ts),
                "median_version_delta": median([t["version_after"] - t["version_before"] for t in ts]),
            })
    return rows


def build_report(meta, results, rows, git_before, git_after, config) -> str:
    all_tasks = [t for b in results for r in b["runs"] for t in r["tasks"]]
    tot_tok = sum(t["input_tokens"] + t["output_tokens"] for t in all_tasks)
    tot_cost = sum(t["cost_usd"] for t in all_tasks)
    leak = (git_before["diff_stat"], git_before["status"]) != (git_after["diff_stat"], git_after["status"])
    iso = all(t["impact"]["other_businesses_identical"] for t in all_tasks)
    L = []
    L.append(f"# Agentic CRM benchmark report\n")
    L.append(f"- Run: `{meta['timestamp']}`  |  mode: **{'DRY RUN (scripted stub, not a model)' if meta['dry_run'] else 'live'}**  |  provider: `{meta['provider']}`  |  model: `{meta['model']}`")
    L.append(f"- Runs per task: {meta['runs']}  |  businesses: {', '.join(meta['businesses'])}  |  task runs: {len(all_tasks)}  |  passed: {sum(t['passed'] for t in all_tasks)}")
    L.append(f"- Tokens: {tot_tok:,}  |  estimated cost: ${tot_cost:.4f} (assumed rates ${config['pricing']['input_per_mtok']}/M in, ${config['pricing']['output_per_mtok']}/M out; replace in tasks.yaml or with --price-in/--price-out)")
    L.append(f"- Wall clock: {meta['wall_seconds']:.0f}s  |  git HEAD: `{git_before['head']}`")
    L.append(f"- **Code leak** (git diff on core code, everything outside scripts/benchmark/ and database files): "
             f"{'**LEAK: core code changed during the run**' if leak else 'none changed during the run'}; "
             f"diff before: {'empty' if not git_before['diff_stat'] and not git_before['status'] else 'NOT EMPTY'}, "
             f"after: {'empty' if not git_after['diff_stat'] and not git_after['status'] else 'NOT EMPTY'}.")
    L.append(f"- **Isolation**: other businesses' records byte-identical across every task: **{'yes' if iso else 'NO'}**.")
    L.append("- Tasks run in order inside one business per run, so a failure early (especially T01) can cascade into later tasks.\n")
    if git_after["diff_stat"] or git_after["status"]:
        L.append("```\n" + (git_after["diff_stat"] or "") + "\n" + (git_after["status"] or "") + "\n```\n")

    L.append("## Summary (task x business)\n")
    L.append("| Task | Business | Pass | Median s | Median tokens | Median cost $ | Owner actions | Unintended record changes | Median version delta |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['task']} | {r['business']} | {r['passed']}/{r['runs']} | {r['median_seconds']} | {r['median_tokens']:,} | "
                 f"{r['median_cost_usd']} | {r['median_owner_actions']:g} | {r['unintended_record_changes']} | {r['median_version_delta']:g} |")
    L.append("\n## Operations (summed over runs)\n")
    L.append("| Task | Business | Proposed | Passed validator | Refused by validator | Applied (approved) | Retries |")
    L.append("|---|---|---|---|---|---|---|")
    for r in rows:
        L.append(f"| {r['task']} | {r['business']} | {r['ops_proposed']} | {r['ops_valid']} | {r['ops_refused']} | {r['ops_applied']} | {r['retries']} |")

    titles = {t["id"]: t["title"] for t in config["tasks"]}
    L.append("\n## Failures\n")
    failures = [(b["business"], r["run"], t) for b in results for r in b["runs"] for t in r["tasks"] if not t["passed"]]
    if not failures:
        L.append("None.")
    else:
        L.append("Classification is a first pass by rule (see `classify` in run_benchmark.py); review before acting on it.\n")
        for biz, run, t in failures:
            L.append(f"- **{t['task']} / {biz} / run {run}** ({titles[t['task']]}) - **{t['classification']}**")
            for c in t["checks"]:
                if not c["ok"]:
                    L.append(f"    - failed `{c['type']}`: {c['reason']}")
            L.append(f"    - why this class: {t['classification_reason']}")
    seed_notes = [(b["business"], r["run"], r["seed"]) for b in results for r in b["runs"] if r["seed"] and r["seed"]["errors"]]
    if seed_notes:
        L.append("\n## Seeding problems (records the harness could not create after T01)\n")
        for biz, run, s in seed_notes:
            L.append(f"- {biz} run {run}: {s['errors'][:3]}")
    L.append("\n## Seeded data\n")
    for b in results:
        for r in b["runs"]:
            if r["seed"]:
                L.append(f"- {b['business']} run {r['run']}: {r['seed']['created']} records in {', '.join(r['seed']['entities']) or 'nothing'}")
    return "\n".join(L) + "\n"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--dry-run", action="store_true", help="use the scripted stub instead of a model")
    p.add_argument("--tasks", type=lambda s: s.split(","), default=None, help="e.g. T01,T02")
    p.add_argument("--businesses", type=lambda s: s.split(","), default=None, help="e.g. gym,bakery")
    p.add_argument("--provider", default=None, help="gemini (default) or anthropic")
    p.add_argument("--model", default=None)
    p.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    p.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")
    p.add_argument("--out", default=os.path.join(HERE, "results"), help="parent directory for the timestamped results folder")
    p.add_argument("--commit", action="store_true",
                   help="after a live, full-suite run: commit only the new results folder and HISTORY files, then push")
    p.add_argument("--backfill", action="store_true",
                   help="add meta.json and a HISTORY.csv row for existing results folders that lack them, then exit")
    return p.parse_args(argv)


def full_suite(args) -> bool:
    return not (args.dry_run or args.tasks or args.businesses)


def write_meta(out_dir, meta):
    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def git_commit_and_push(out_dir, results_root, meta, pass_rate, branch):
    """Commits exactly the new results folder and the history files (nothing
    else in the tree), then pushes the current branch. No trailers."""
    rel = lambda p: os.path.relpath(p, REPO).replace(os.sep, "/")
    paths = [rel(out_dir), rel(os.path.join(results_root, history.CSV_NAME)), rel(os.path.join(results_root, history.MD_NAME))]
    msg = f"benchmark: {meta['timestamp']} {meta['model']} pass {pass_rate * 100:.1f}%"
    for cmd in (["git", "add", "--", *paths], ["git", "commit", "-m", msg, "--", *paths],
                ["git", "push", "origin", branch]):
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"{' '.join(cmd[:2])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    print(f"Committed and pushed: {msg}")


def backfill(results_root):
    """Gives older results folders a meta.json and a HISTORY row. Reads only
    what the run itself recorded; the branch is the one checked out now."""
    tree = tree_state()
    added = 0
    for stamp in sorted(os.listdir(results_root)):
        folder = os.path.join(results_root, stamp)
        path = os.path.join(folder, "results.json")
        if not os.path.exists(path):
            continue
        data = json.load(open(path, encoding="utf-8"))
        old = data["meta"]
        full = subprocess.run(["git", "rev-parse", data["git_before"]["head"]], cwd=REPO,
                              capture_output=True, text=True).stdout.strip() or data["git_before"]["head"]
        dirty_files = [l for l in data["git_before"]["status"].splitlines() if l]
        pricing = old.get("pricing", {})
        tasks = sorted({t["task"] for b in data["businesses"] for r in b["runs"] for t in r["tasks"]})
        meta = {"timestamp": old["timestamp"], "commit": full, "branch": tree["branch"],
                "dirty_tree": bool(dirty_files), "dirty_files": dirty_files,
                "provider": old["provider"], "model": old["model"], "system_prompt_sha256": system_prompt_hash(),
                "runs": old["runs"], "businesses": old["businesses"], "tasks": tasks, "dry_run": old["dry_run"],
                "prices": {**pricing, "basis": pricing.get("basis", "assumed")}, "wall_seconds": old.get("wall_seconds"),
                "backfilled": True,
                "backfill_note": "meta.json written after the fact from results.json; branch is the branch checked out at backfill; "
                                 "system prompt hash computed from the current code (agentic/chat.py unchanged since the run's commit)."}
        if not os.path.exists(os.path.join(folder, "meta.json")):
            write_meta(folder, meta)
        row = history.build_row(old, data["businesses"], data["git_before"], data["git_after"], full, bool(dirty_files))
        if not old["dry_run"] and history.append_row(results_root, row):
            added += 1
            print(f"logged {stamp}")
    history.write_markdown(results_root)
    print(f"Backfill done: {added} row(s) added.")


def main(argv=None):
    args = parse_args(argv)
    if args.backfill:
        backfill(args.out)
        return None
    if args.commit and not full_suite(args):
        sys.exit("--commit needs a live, full-suite run (no --dry-run, --tasks or --businesses), "
                 "so the permanent log only holds comparable runs.")
    config = yaml.safe_load(open(TASKS_FILE, encoding="utf-8"))
    if args.price_in is not None:
        config["pricing"]["input_per_mtok"] = args.price_in
    if args.price_out is not None:
        config["pricing"]["output_per_mtok"] = args.price_out
    h = Harness(args, config)
    h.setup_app()
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(args.out, stamp)
    os.makedirs(out_dir, exist_ok=True)

    tree = tree_state()
    git_before = git_state()
    t0 = time.monotonic()
    results = run_all(h, config, args)
    wall = time.monotonic() - t0
    git_after = git_state()

    models = sorted({t["model"] for b in results for r in b["runs"] for t in r["tasks"] if t["model"]})
    meta = {"timestamp": stamp, "dry_run": args.dry_run, "runs": args.runs, "provider": os.environ["LLM_PROVIDER"],
            "model": ", ".join(models) or "(none recorded)", "businesses": [b["business"] for b in results],
            "wall_seconds": wall, "pricing": config["pricing"]}
    rows = summarize(results, config, args)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "git_before": git_before, "git_after": git_after,
                   "summary": rows, "businesses": results}, f, indent=2, default=str)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(build_report(meta, results, rows, git_before, git_after, config))
    write_meta(out_dir, {
        "timestamp": stamp, "commit": tree["commit"], "branch": tree["branch"],
        "dirty_tree": tree["dirty"], "dirty_files": tree["dirty_files"],
        "provider": meta["provider"], "model": meta["model"], "system_prompt_sha256": system_prompt_hash(),
        "runs": args.runs, "businesses": meta["businesses"],
        "tasks": [t["id"] for t in config["tasks"] if not args.tasks or t["id"] in args.tasks],
        "dry_run": args.dry_run, "full_suite": full_suite(args),
        "prices": price_meta(config, args), "wall_seconds": round(wall, 1), "backfilled": False})
    print(f"\nResults: {out_dir}")

    if full_suite(args):
        row = history.build_row(meta, results, git_before, git_after, tree["commit"], tree["dirty"])
        history.append_row(args.out, row)
        history.write_markdown(args.out)
        print(f"Logged to {os.path.join(args.out, history.CSV_NAME)}")
        if args.commit:
            git_commit_and_push(out_dir, args.out, meta, float(row["pass_rate"]), tree["branch"])
    else:
        print("Not logged to HISTORY (dry run or filtered run); only full live runs are comparable.")
    return out_dir


if __name__ == "__main__":
    main()
