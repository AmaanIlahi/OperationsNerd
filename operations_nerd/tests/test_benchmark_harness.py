"""
The benchmark harness itself (scripts/benchmark/run_benchmark.py), tested
without a model: a full --dry-run in a subprocess (so it gets its own
throwaway database), plus unit checks that the harness's checks can actually
fail. Run from operations_nerd/:
    python tests/test_benchmark_harness.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
BENCH = os.path.join(REPO, "scripts", "benchmark")
sys.path.insert(0, BENCH)

import run_benchmark as rb


def spec(*entities, links=()):
    return {"business": {"name": "x", "industry": None}, "entities": list(entities), "links": list(links)}


def entity(key, label, *fields, archived=False):
    return {"key": key, "label": label, "archived": archived, "fields": list(fields)}


def field(key, label, ftype="text", archived=False, **extra):
    return {"key": key, "label": label, "type": ftype, "required": False, "archived": archived, **extra}


def ctx(before, after, **kw):
    snaps = kw.pop("snapshots", [after])
    return rb.Ctx(biz={"expect_entities": [["Member"], ["Class"]]}, before_spec=before, snapshots=snaps,
                  steps=kw.pop("steps", [{}]), version_before=1, version_after=kw.pop("version_after", 2),
                  records_in=kw.pop("records_in", lambda k: 0), **kw)


# ---------- the harness runs end to end ----------

def test_dry_run_completes_and_writes_results():
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([sys.executable, os.path.join(BENCH, "run_benchmark.py"), "--dry-run", "--runs", "1",
                            "--out", out], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
        (stamp,) = os.listdir(out)
        data = json.load(open(os.path.join(out, stamp, "results.json"), encoding="utf-8"))
        report = open(os.path.join(out, stamp, "report.md"), encoding="utf-8").read()

    assert data["meta"]["dry_run"] is True and data["meta"]["provider"] == "stub"
    assert [b["business"] for b in data["businesses"]] == ["gym", "groomer", "bakery"]
    tasks = [t for b in data["businesses"] for run in b["runs"] for t in run["tasks"]]
    assert len(tasks) == 3 * 10, len(tasks)
    failed = [(t["task"], [c for c in t["checks"] if not c["ok"]]) for t in tasks if not t["passed"]]
    assert not failed, failed                                    # the scripted agent can do every task
    for t in tasks:
        assert t["input_tokens"] > 0 and t["owner_actions"] >= 1 and t["unintended_record_changes"] == 0
        assert t["impact"]["other_businesses_identical"] is True
    assert all(run["seed"]["created"] > 0 for b in data["businesses"] for run in b["runs"])
    assert data["git_before"]["diff_stat"] == data["git_after"]["diff_stat"]
    assert data["git_before"]["status"] == data["git_after"]["status"]       # code leak: none
    assert len(data["summary"]) == 30
    for needle in ("DRY RUN", "Code leak", "Isolation", "## Summary", "## Operations", "## Failures"):
        assert needle in report, needle

    # the refusal paths were really exercised: T09 was refused by the validator, T10 needed approval
    t09 = next(t for t in tasks if t["task"] == "T09")
    assert t09["ops_refused"] >= 2 and t09["ops_applied"] == 0 and t09["retries"] >= 1
    t10 = next(t for t in tasks if t["task"] == "T10")
    assert t10["steps"][0]["status"] == "pending" and t10["steps"][0]["decision"] == "reject"


def test_dry_run_does_not_touch_the_real_database():
    import sqlite3
    real = os.path.join(HERE, "..", "db", "operations_nerd.db")
    before = os.path.getmtime(real) if os.path.exists(real) else None
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run([sys.executable, os.path.join(BENCH, "run_benchmark.py"), "--dry-run", "--runs", "1",
                            "--businesses", "gym", "--tasks", "T01,T02", "--out", out],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-1500:]
    assert (os.path.getmtime(real) if os.path.exists(real) else None) == before


# ---------- the checks can fail ----------

def test_spec_diff_and_only_changes():
    a = spec(entity("e1", "Member", field("f1", "Name"), field("f2", "Phone")))
    b = copy.deepcopy(a)
    b["entities"][0]["fields"].append(field("f3", "Referral", "select", options=["Walk-in"]))
    b["entities"][0]["fields"][1]["label"] = "Mobile"
    d = rb.spec_diff(a, b)
    assert d["fields_added"] == [("e1", "f3")] and d["fields_changed"] == [("e1", "f2")]
    ok, _ = rb.ck_only_changes(ctx(a, b), {"allow": ["fields_added"]})
    assert not ok
    ok, _ = rb.ck_only_changes(ctx(a, b), {"allow": ["fields_added", "fields_changed"]})
    assert ok


def test_field_added_checks_label_type_and_options():
    a = spec(entity("e1", "Member", field("f1", "Name")))
    good = copy.deepcopy(a)
    good["entities"][0]["fields"].append(field("f2", "Referral source", "select", options=["Walk-in", "Referral", "Online"]))
    p = {"entity": "e1", "label_contains": "referral", "field_type": "select", "options_include": ["Walk-in", "Online"]}
    assert rb.ck_field_added(ctx(a, good), p)[0]
    wrong_type = copy.deepcopy(good)
    wrong_type["entities"][0]["fields"][1]["type"] = "text"
    assert not rb.ck_field_added(ctx(a, wrong_type), p)[0]
    missing_opt = copy.deepcopy(good)
    missing_opt["entities"][0]["fields"][1]["options"] = ["Walk-in"]
    assert not rb.ck_field_added(ctx(a, missing_opt), p)[0]
    assert not rb.ck_field_added(ctx(a, a), p)[0]


def test_rename_keeps_key_or_fails():
    a = spec(entity("e1", "Member", field("f1", "Name"), field("f2", "Phone")))
    renamed = copy.deepcopy(a)
    renamed["entities"][0]["fields"][1]["label"] = "Preferred Phone"
    p = {"entity": "e1", "field": "f2", "new_label": "Preferred Phone"}
    assert rb.ck_field_renamed(ctx(a, renamed), p)[0]
    assert not rb.ck_field_renamed(ctx(a, a), p)[0]
    new_key = copy.deepcopy(renamed)
    new_key["entities"][0]["fields"][1]["key"] = "f9"                      # a new key instead of a rename
    assert not rb.ck_field_renamed(ctx(a, new_key), p)[0]


def test_round_trip_and_archive_checks():
    a = spec(entity("e1", "Member", field("f1", "Name"), field("f2", "Phone")))
    gone = copy.deepcopy(a)
    gone["entities"][0]["fields"][1]["archived"] = True
    assert rb.ck_field_archived(ctx(a, a, snapshots=[gone, a]), {"entity": "e1", "field": "f2", "after_step": 1})[0]
    assert not rb.ck_field_archived(ctx(a, a, snapshots=[a, a]), {"entity": "e1", "field": "f2", "after_step": 1})[0]
    assert rb.ck_spec_equals_task_start(ctx(a, a, snapshots=[gone, a]), {})[0]
    assert not rb.ck_spec_equals_task_start(ctx(a, gone), {})[0]
    assert not rb.ck_field_active(ctx(a, gone), {"entity": "e1", "field": "f2"})[0]


def test_link_and_entity_checks():
    a = spec(entity("e1", "Member", field("f1", "Name")), entity("e2", "Class", field("f2", "Name")))
    b = copy.deepcopy(a)
    b["links"].append({"key": "l1", "label": "Attends", "from": "e1", "to": "e2", "cardinality": "many_to_one", "archived": False})
    assert rb.ck_link_between(ctx(a, b), {"a": "e2", "b": "e1"})[0]                # either direction
    assert not rb.ck_link_between(ctx(a, a), {"a": "e1", "b": "e2"})[0]
    b["links"][0]["archived"] = True
    assert not rb.ck_link_between(ctx(a, b), {"a": "e1", "b": "e2"})[0]
    c = copy.deepcopy(a)
    c["entities"].append(entity("e3", "Feedback", field("f3", "Rating", "number")))
    assert rb.ck_entity_added(ctx(a, c), {"label_contains": "feedback", "min_fields": 1})[0]
    assert not rb.ck_entity_added(ctx(a, a), {"label_contains": "feedback"})[0]
    assert rb.ck_entities_present(ctx(a, a), {})[0]
    only_member = spec(entity("e1", "Member", field("f1", "Name")))
    assert not rb.ck_entities_present(ctx(only_member, only_member), {})[0]


def test_impact_detects_changed_lost_added_and_cross_business_leaks():
    before = {(1, 10): "a", (1, 11): "b", (2, 20): "c"}
    assert rb.impact(before, dict(before), 1) == {"records_before": 2, "changed": [], "lost": [], "added": [],
                                                  "other_businesses_identical": True}
    after = {(1, 10): "CHANGED", (1, 12): "new", (2, 20): "c"}                # 10 changed, 11 lost, 12 added
    imp = rb.impact(before, after, 1)
    assert (imp["changed"], imp["lost"], imp["added"]) == ([10], [11], [12]) and imp["other_businesses_identical"]
    assert rb.impact(before, {**before, (2, 20): "x"}, 1)["other_businesses_identical"] is False


def test_classification_rules():
    base = {"task": "T02", "steps": [{}], "errors": [], "checks": [{"type": "field_added", "ok": False, "reason": "r"}]}
    assert rb.classify({**base, "precondition_failed": True})[0] == rb.GAP
    assert rb.classify({**base, "checks": [{"type": "no_unintended_record_changes", "ok": False, "reason": "r"}]})[0] == rb.BUG
    assert rb.classify({**base, "steps": [{"http": 500}]})[0] == rb.BUG
    assert rb.classify({**base, "steps": [{"http": 200, "applied_ops": [], "refused": []}]})[0] == rb.GAP
    refused = {**base, "steps": [{"http": 200, "applied_ops": [], "refused": [{"op": "add_field", "reasons": ["'label' is required"]}]}]}
    label, why = rb.classify(refused)
    assert label == rb.GAP and "label" in why
    t09 = {"task": "T09", "steps": [{"http": 200}], "errors": [], "checks": [{"type": "spec_unchanged", "ok": False, "reason": "r"}]}
    assert rb.classify(t09)[0] == rb.BUG


def test_selectors_pick_real_targets_or_none():
    biz = {"primary": ["Member"]}
    s = spec(entity("e1", "Member", field("f1", "Name"), field("f2", "Notes"), field("f3", "Phone", "phone"),
                    field("f4", "Level", "select", options=["a"])),
             entity("e2", "Class", field("f5", "Title")))
    assert rb.SELECTORS["primary_entity"](s, biz)["entity_key"] == "e1"
    assert rb.SELECTORS["retypable_field"](s, biz)["field_key"] == "f2"          # first non-first text field
    assert rb.SELECTORS["renamable_field"](s, biz)["new_label"] == "Preferred Notes"
    a, b = rb.SELECTORS["removable_field_a"](s, biz), rb.SELECTORS["removable_field_b"](s, biz)
    assert a["field_key"] != b["field_key"] and "f1" not in (a["field_key"], b["field_key"])
    assert rb.SELECTORS["unlinked_pair"](s, biz)["entity_b_key"] == "e2"
    assert rb.SELECTORS["retypable_field"](spec(entity("e1", "Member", field("f1", "Name"))), biz) is None
    assert rb.SELECTORS["primary_entity"](spec(), biz) is None


def test_task_file_is_well_formed():
    import yaml
    cfg = yaml.safe_load(open(os.path.join(BENCH, "tasks.yaml"), encoding="utf-8"))
    assert [t["id"] for t in cfg["tasks"]] == [f"T{i:02d}" for i in range(1, 11)]
    assert [b["id"] for b in cfg["businesses"]] == ["gym", "groomer", "bakery"]
    assert cfg["businesses"][0]["template"] == "health_club" and cfg["businesses"][1]["template"] is None
    for t in cfg["tasks"]:
        assert t["steps"] and t["checks"], t["id"]
        assert all(c["type"] in rb.CHECKS for c in t["checks"]), t["id"]
        assert not t.get("bind") or t["bind"] in rb.SELECTORS, t["id"]
        assert all(("say" in s) != ("action" in s) for s in t["steps"]), t["id"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
