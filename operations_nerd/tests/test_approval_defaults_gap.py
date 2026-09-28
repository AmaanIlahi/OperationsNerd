"""
Approval defaults: current behaviour vs intended behaviour.

Background
----------
Every pack ships an approval_defaults.yaml, and the loader validates it: every
action type an event can produce must have a default, and every default must
correspond to a real action type. But nothing ever writes those defaults into
the approval_policies table. create_business does not, and no other code path
calls set_approval_policy outside the tests.

The effect: a pack can declare escalate_to_manager: true and the action still
requires manual approval. It fails safe, since the fallback is ask-first, so
nothing unsafe happens. But a documented pack setting has no runtime effect.

What this test does
-------------------
Part A records the CURRENT behaviour, so the gap is pinned down rather than
described.

Part B demonstrates the INTENDED behaviour using a seeding helper defined here
in the test, not in the core. It shows what business creation would do if it
seeded policies from the pack, and asserts that every declared default then
takes effect.

Nothing in the core is modified. Whether create_business should seed these
policies is an open decision for Amaan and Prof. Shasha -- see
BRANCH_NOTES.md. If that decision is taken, Part B becomes the specification
for it and Part A is the test that should then be deleted.

Run:  python operations_nerd/tests/test_approval_defaults_gap.py
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
    return [n for n in sorted(os.listdir(PACKS_DIR))
            if os.path.isdir(os.path.join(PACKS_DIR, n))
            and os.path.exists(os.path.join(PACKS_DIR, n, "pack.yaml"))]


def seed_approval_policies_from_pack(conn, business_id, pack):
    """
    PROPOSED behaviour, defined here rather than in the core.

    This is the whole change under discussion: after creating a business,
    write the pack's declared approval defaults into approval_policies so that
    what the pack says is what the system does. Three lines.
    """
    for action_type, auto_approve in pack.approval_defaults.items():
        db.set_approval_policy(conn, business_id, action_type,
                               auto_approve=bool(auto_approve))


def part_a_current_behaviour(pack_id, pack, db_path):
    """The gap, pinned down."""
    print(f"\n  [A] {pack_id}: current behaviour")
    with db.get_conn(db_path) as conn:
        business_id = db.create_business(
            conn, name=f"A-{pack_id}", industry_pack=pack_id,
            pack_version=pack.version, settings={})

        declared_true = [a for a, v in pack.approval_defaults.items() if v]
        check(f"    [{pack_id}] pack declares at least one auto-approve default",
              len(declared_true) > 0,
              "nothing to demonstrate for this pack" if not declared_true else "")

        for action_type in pack.approval_defaults:
            actual = db.is_auto_approved(conn, business_id, action_type)
            check(f"    [{pack_id}] {action_type}: fresh business gates it "
                  f"(pack says {pack.approval_defaults[action_type]})",
                  actual is False, f"got {actual}")

        n = conn.execute(
            "SELECT COUNT(*) FROM approval_policies WHERE business_id=?",
            (business_id,)).fetchone()[0]
        check(f"    [{pack_id}] create_business writes no approval policies at all",
              n == 0, f"found {n}")


def part_b_intended_behaviour(pack_id, pack, db_path):
    """What it should do, demonstrated without changing the core."""
    print(f"\n  [B] {pack_id}: intended behaviour, with seeding")
    with db.get_conn(db_path) as conn:
        business_id = db.create_business(
            conn, name=f"B-{pack_id}", industry_pack=pack_id,
            pack_version=pack.version, settings={})

        seed_approval_policies_from_pack(conn, business_id, pack)

        for action_type, declared in pack.approval_defaults.items():
            actual = db.is_auto_approved(conn, business_id, action_type)
            check(f"    [{pack_id}] {action_type}: honoured as declared ({declared})",
                  bool(actual) == bool(declared), f"got {actual}")

        n = conn.execute(
            "SELECT COUNT(*) FROM approval_policies WHERE business_id=?",
            (business_id,)).fetchone()[0]
        check(f"    [{pack_id}] one policy row per declared default",
              n == len(pack.approval_defaults),
              f"expected {len(pack.approval_defaults)}, got {n}")

        # the owner can still override afterwards, which is the point of the
        # questionnaire path -- seeding sets the starting position, not a lock
        first = next(iter(pack.approval_defaults))
        flipped = not bool(pack.approval_defaults[first])
        db.set_approval_policy(conn, business_id, first, auto_approve=flipped)
        check(f"    [{pack_id}] owner can still override a seeded default",
              bool(db.is_auto_approved(conn, business_id, first)) == flipped)


def main():
    db_path = os.path.join(tempfile.mkdtemp(), "approval_defaults.db")
    db.init_db(db_path, reset=True)

    packs = discover_packs()
    print("=" * 74)
    print("APPROVAL DEFAULTS: current vs intended")
    print(f"packs on disk: {packs}")
    print("=" * 74)

    for pack_id in packs:
        pack = loader.load_pack(pack_id)
        part_a_current_behaviour(pack_id, pack, db_path)
        part_b_intended_behaviour(pack_id, pack, db_path)

    print("\n" + "=" * 74)
    if failures:
        print(f"{len(failures)} FAILED of {checks} checks:")
        for f in failures:
            print(f"  - {f}")
        print("=" * 74)
        sys.exit(1)
    print(f"ALL {checks} CHECKS PASSED")
    print("Part A documents the gap. Part B is the proposed fix, demonstrated")
    print("without touching the core. Decision pending with Amaan.")
    print("=" * 74)


if __name__ == "__main__":
    main()