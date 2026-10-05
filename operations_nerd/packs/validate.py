"""Check every pack folder: python -m packs.validate  (exit 1 if any pack is broken)."""
import os
import sys

from packs.loader import load_pack, PackLoadError

HERE = os.path.dirname(__file__)


def pack_ids() -> list[str]:
    return sorted(n for n in os.listdir(HERE) if os.path.isfile(os.path.join(HERE, n, "pack.yaml")))


def check_all() -> list[dict]:
    out = []
    for pid in pack_ids():
        try:
            p = load_pack(pid)
            out.append({"id": pid, "ok": True, "version": p.version, "display_name": p.display_name, "issues": []})
        except PackLoadError as e:
            out.append({"id": pid, "ok": False, "issues": e.issues})
    return out


if __name__ == "__main__":
    res = check_all()
    for r in res:
        print(("OK   " if r["ok"] else "FAIL ") + r["id"])
        for i in r["issues"]:
            print("   - " + i)
    sys.exit(0 if all(r["ok"] for r in res) else 1)
