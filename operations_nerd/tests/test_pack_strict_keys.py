"""
Strict pack-loading keys.

Until this change, two kinds of mistakes vanished silently:

1. An unknown top-level block in a pack file (for example record_types:
   in pack.yaml) was read and then dropped, because only the known keys
   are ever passed to the Pack model. No error, no warning.
2. A misspelled or unsupported key inside a known block (a question, an
   event schema, an entity schema) was dropped by pydantic's default
   extra='ignore'.

Both hide pack-authoring bugs and, worse, make a genuine break in the
config-only thesis look like a quiet no-op. The loader's own docstring
says a loader that silently tolerates bad packs makes that distinction
meaningless, so the loader now rejects both. These checks build throwaway
pack folders under the system temp dir; the two shipped packs are checked
last and must keep loading.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from packs.loader import load_pack, PackLoadError

PASS, FAIL = 0, 0


def check(label, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ok        {label}")
    except AssertionError as e:
        FAIL += 1
        print(f"  FAILED    {label}: {e}")


BASE_FILES = {
    "pack.yaml": "id: t\nversion: 0.1.0\ndisplay_name: T\n",
    "questionnaire.yaml": "questions:\n  - id: q1\n    label: Q\ntype: text\n    settings_key: x\n".replace("Q\ntype", "Q1\n    type"),
    "event_schemas.yaml": "schemas:\n  - event_type: inbound_email\n    extract_fields: [intent]\n    valid_action_types: [send_reply]\n",
    "prompts.yaml": "templates:\n  extract: 'hi {{settings.x}}'\n",
    "approval_defaults.yaml": "defaults:\n  send_reply: false\n",
}


def make_pack(overrides):
    d = tempfile.mkdtemp()
    pack_dir = os.path.join(d, "t")
    os.mkdir(pack_dir)
    for fname, content in {**BASE_FILES, **overrides}.items():
        with open(os.path.join(pack_dir, fname), "w") as f:
            f.write(content)
    return d


def expect_load_error(overrides, needle):
    d = make_pack(overrides)
    try:
        load_pack("t", packs_dir=d)
    except PackLoadError as e:
        assert any(needle in i for i in e.issues), f"expected issue containing {needle!r}, got {e.issues}"
        return
    finally:
        shutil.rmtree(d, ignore_errors=True)
    raise AssertionError("pack loaded without error")


print("Unknown top-level blocks are reported, not dropped")
check("record_types block in pack.yaml is rejected",
      lambda: expect_load_error({"pack.yaml": BASE_FILES["pack.yaml"] + "record_types:\n  - name: item\n"},
                                "unknown top-level key 'record_types'"))
check("unknown key in approval_defaults.yaml is rejected",
      lambda: expect_load_error({"approval_defaults.yaml": BASE_FILES["approval_defaults.yaml"] + "extra_block: 1\n"},
                                "unknown top-level key 'extra_block'"))
check("unknown key in entity_schemas.yaml is rejected",
      lambda: expect_load_error({"entity_schemas.yaml": "entities: []\nother: 1\n"},
                                "unknown top-level key 'other'"))

print("Misspelled or unsupported keys inside known blocks are rejected")
bad_question = ("questions:\n  - id: q1\n    label: Q1\n    type: text\n"
                "    settings_key: x\n    settings_keys: x\n")
check("typo key inside a question is rejected",
      lambda: expect_load_error({"questionnaire.yaml": bad_question}, "settings_keys"))
bad_schema = ("schemas:\n  - event_type: inbound_email\n    extract_fields: [intent]\n"
              "    valid_action_types: [send_reply]\n    extract_field: [intent]\n")
check("typo key inside an event schema is rejected",
      lambda: expect_load_error({"event_schemas.yaml": bad_schema}, "extract_field"))
bad_entity = "entities:\n  - entity_type: item\n    label: Item\n    fields: [sku]\n    record_types: []\n"
check("unsupported key inside an entity schema is rejected",
      lambda: expect_load_error({"entity_schemas.yaml": bad_entity}, "record_types"))

print("Clean packs keep loading")
check("a minimal valid pack still loads",
      lambda: (lambda d: (load_pack("t", packs_dir=d), shutil.rmtree(d, ignore_errors=True)))(make_pack({})))

here = os.path.dirname(__file__)
for pack_id in ("healthclub", "realestate"):
    check(f"shipped pack '{pack_id}' still loads",
          lambda pack_id=pack_id: load_pack(pack_id, packs_dir=os.path.join(here, "..", "packs")))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
