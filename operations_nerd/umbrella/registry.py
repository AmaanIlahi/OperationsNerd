"""Discover trusted, installed Nerd packages; owners never upload executable code."""
from importlib import import_module
from pathlib import Path
import re

ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ROOT = Path(__file__).resolve().parents[1] / "modules"


def manifest(nerd_id):
    if not isinstance(nerd_id, str) or not ID.fullmatch(nerd_id) or not (ROOT / nerd_id / "manifest.py").is_file():
        raise ValueError("This capability is not installed")
    value = import_module("modules." + nerd_id + ".manifest")
    if getattr(value, "ID", None) != nerd_id:
        raise ValueError("Capability manifest identity does not match its package")
    return value


def installed_modules():
    return [name.name for name in sorted(ROOT.iterdir()) if name.is_dir() and ID.fullmatch(name.name)
            and (name / "manifest.py").is_file()]


def adapter(nerd_id):
    manifest(nerd_id)
    # Logistics keeps its existing tested host seam; the generic lifecycle and
    # other modules use the same adapter interface without calling Logistics.
    return import_module("umbrella.logistics_adapter" if nerd_id == "logistics" else "modules." + nerd_id + ".adapter")


def migrator(nerd_id):
    manifest(nerd_id)
    return import_module("modules." + nerd_id + ".migrate")


def assert_record_writable(conn, business_id, entity_key, values, record_id=None):
    from agentic.records import RecordError
    rows = conn.execute("SELECT nerd_id,status FROM nerd_installations").fetchall()
    for row in rows:
        try:
            module = adapter(row["nerd_id"])
        except (ImportError, ValueError):
            if row["status"] in ("enabled", "enabling"):
                raise RecordError(409, "An enabled capability is unavailable; restore its code before editing its records") from None
            continue
        hook = getattr(module, "assert_record_writable", None)
        if hook:
            hook(conn, business_id, entity_key, values, record_id=record_id)
