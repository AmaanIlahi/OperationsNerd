"""
Pack compatibility regression test.

Purpose: prove that the existing verticals load and run exactly as before, so
that when the records/links extension is eventually merged we have a concrete
before-and-after baseline rather than an assumption.

This test is READ-ONLY with respect to the core. It imports the real loader and
the real db helpers, loads every pack directory found on disk without modifying
it, and runs each one through a minimal end-to-end flow in a throwaway
database.

Written as a plain script with assertions, matching the existing convention in
this directory (no pytest dependency is added).

Run:  python operations_nerd/tests/test_pack_compat_smoke.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, ROOT)

from operations_nerd.db import db
from operations_nerd.packs import loader

PACKS_DIR = os.path.join(ROOT, "operations_nerd", "packs")

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def discover_packs():
    """Every directory under packs/ that contains a pack.yaml."""
    found = []
    for name in sorted(os.listdir(PACKS_DIR)):
        path = os.path.join(PACKS_DIR, name)
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "pack.yaml")):
            found.append(name)
    return found


def test_pack_loads(pack_id):
    """The pack loads, and its declared surface is intact."""
    pack = loader.load_pack(pack_id)

    check(f"[{pack_id}] loads without error", pack is not None)
    check(f"[{pack_id}] id matches directory name", pack.id == pack_id,
          f"got {pack.id!r}")
    check(f"[{pack_id}] declares a version", bool(pack.version))
    check(f"[{pack_id}] has questions", len(pack.questions) > 0,
          f"got {len(pack.questions)}")
    check(f"[{pack_id}] has event schemas", len(pack.event_schemas) > 0,
          f"got {len(pack.event_schemas)}")

    # every question maps to a settings key
    bad = [q.id for q in pack.questions if not getattr(q, "settings_key", None)]
    check(f"[{pack_id}] every question has a settings_key", not bad, f"missing on {bad}")

    # every action an event can produce has an approval default
    declared = {a for es in pack.event_schemas for a in es.valid_action_types}
    missing = declared - set(pack.approval_defaults.keys())
    check(f"[{pack_id}] every valid_action_type has an approval default",
          not missing, f"missing {sorted(missing)}")

    # every event schema extracts at least one field, and all are plain strings
    for es in pack.event_schemas:
        check(f"[{pack_id}] {es.event_type} extracts fields",
              len(es.extract_fields) > 0)
        check(f"[{pack_id}] {es.event_type} extract_fields are all strings",
              all(isinstance(f, str) for f in es.extract_fields))

    return pack


def test_pack_runs(pack_id, pack, db_path):
    """A minimal end-to-end flow, the same shape the verticals rely on today."""
    with db.get_conn(db_path) as conn:
        business_id = db.create_business(
            conn, name=f"Compat test for {pack_id}", industry_pack=pack_id,
            pack_version=pack.version, settings={"compat": True})
        check(f"[{pack_id}] business created", business_id is not None)

        contact_id = db.create_contact(
            conn, business_id, name="Compat Contact", email="compat@example.test",
            extension_data={"source": "compat_test"})
        got = db.get_contact(conn, contact_id)
        check(f"[{pack_id}] contact + extension_data round-trips",
              got["extension_data"].get("source") == "compat_test")

        follow_up_id = db.create_follow_up(
            conn, business_id, contact_id=contact_id, type_="compat_check",
            extension_data={"note": "still works"})
        check(f"[{pack_id}] follow_up created against the contact",
              follow_up_id is not None)

        found = db.find_entity_ids_by_attribute(conn, "contact", "source", "compat_test")
        check(f"[{pack_id}] attribute query still finds the contact",
              contact_id in found)

        # the action path, using an action type this pack actually declares
        action_type = next(iter(
            {a for es in pack.event_schemas for a in es.valid_action_types}), None)
        if action_type:
            event_id = db.create_event(conn, business_id, source="form",
                                       raw_content="compat test event")
            action_id = db.create_drafted_action(
                conn, event_id, business_id, action_type=action_type,
                payload={"compat": True})
            action = db.get_drafted_action(conn, action_id)
            check(f"[{pack_id}] drafted action created ({action_type})",
                  action is not None)
            check(f"[{pack_id}] action starts pending approval",
                  action["status"] == "pending_approval",
                  f"got {action['status']!r}")

            # NOTE: the pack's approval_defaults are validated by the loader but
            # never written into approval_policies by any code path -- nothing
            # outside the tests calls set_approval_policy. So a fresh business
            # has no policy and is_auto_approved falls back to False (ask first).
            # That is the current behaviour, so the baseline records it rather
            # than failing on it. Flagged as an open question, not changed here.
            expected_from_pack = pack.approval_defaults.get(action_type, False)
            actual = db.is_auto_approved(conn, business_id, action_type)
            check(f"[{pack_id}] fresh business gates {action_type} (ask-first baseline)",
                  actual is False,
                  f"pack default is {expected_from_pack}, db returned {actual}")
            if expected_from_pack and not actual:
                print(f"        note: pack declares {action_type}=True but no code "
                      f"applies approval_defaults to a business")

            # and the policy mechanism itself still works when something sets it
            db.set_approval_policy(conn, business_id, action_type, auto_approve=True)
            check(f"[{pack_id}] approval policy is honoured once set",
                  db.is_auto_approved(conn, business_id, action_type) is True)


def test_record_types_absent_today(pack_id, pack):
    """
    Documents current behaviour, so the extension has a baseline to change.

    No existing pack declares record_types. Today an unknown key is silently
    dropped by pydantic's default extra='ignore' -- which is itself a finding
    (see WAREHOUSE_FAILURE_NOTES.md step 11). This test records the status quo;
    it should be updated, deliberately, when validation is added.
    """
    check(f"[{pack_id}] declares no record_types (baseline)",
          not hasattr(pack, "record_types"))


def main():
    db_path = os.path.join(tempfile.mkdtemp(), "pack_compat.db")
    db.init_db(db_path, reset=True)

    packs = discover_packs()
    print("=" * 70)
    print(f"PACK COMPATIBILITY: found {len(packs)} pack(s) on disk: {packs}")
    print("=" * 70)

    if not packs:
        print("\nNo packs found. Something is wrong with the checkout.")
        sys.exit(1)

    for pack_id in packs:
        print(f"\n--- {pack_id} ---")
        pack = test_pack_loads(pack_id)
        test_record_types_absent_today(pack_id, pack)
        test_pack_runs(pack_id, pack, db_path)

    print("\n" + "=" * 70)
    if failures:
        print(f"{len(failures)} FAILED of {checks} checks:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 70)
        sys.exit(1)
    print(f"ALL {checks} CHECKS PASSED across {len(packs)} pack(s)")
    print("=" * 70)


if __name__ == "__main__":
    main()