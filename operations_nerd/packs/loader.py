"""
Config Pack loader.

Job: take a pack folder (e.g. packs/realestate/) and return one validated
Pack object that the rest of the system reads from. Nothing downstream
should ever open a YAML file directly, it should ask this module.

Two layers of validation, deliberately kept separate:

1. Structural validation (via Pydantic): does each file have the right
   shape? Right field names, right types, required fields present?
   This is automatic, it comes from the type hints on the models below.

2. Cross-file validation (hand-written, in validate_pack): is the pack
   internally consistent? Does every settings_key a prompt references
   actually exist in the questionnaire? Does every action_type an event
   schema can produce have a matching approval default?
   Pydantic can't catch this on its own, since it validates one file's
   shape at a time and has no idea what's inside a different file.

Both layers matter for the research method: a pack that fails to load
here is either a pack-authoring bug (fixable by editing the pack) or,
if it can't be fixed by editing the pack, a genuine break in the
config-only thesis. A loader that silently tolerated bad packs would
make that distinction meaningless.
"""

import os
import re
import yaml
from pydantic import BaseModel, ValidationError


# ---------- structural models ----------

class Question(BaseModel):
    id: str
    label: str
    type: str                      # text | number | multiselect | boolean | select
    settings_key: str
    options: list[str] | None = None


class EventSchema(BaseModel):
    event_type: str
    extract_fields: list[str]
    valid_action_types: list[str]


class Pack(BaseModel):
    id: str
    version: str
    display_name: str
    questions: list[Question]
    event_schemas: list[EventSchema]
    prompts: dict[str, str]
    approval_defaults: dict[str, bool]


class PackLoadError(Exception):
    """Raised when a pack fails to parse or fails cross-file validation.
    Carries every problem found, not just the first one, so a pack author
    (or the falsifiability log) gets the full picture in one pass."""
    def __init__(self, pack_id: str, issues: list[str]):
        self.pack_id = pack_id
        self.issues = issues
        message = f"Pack '{pack_id}' failed to load:\n" + "\n".join(f"  - {i}" for i in issues)
        super().__init__(message)


# ---------- loading ----------

def _read_yaml(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def load_pack(pack_id: str, packs_dir: str = None) -> Pack:
    """Reads packs/<pack_id>/*.yaml, builds a Pack, and validates it.
    Raises PackLoadError on any structural or cross-file problem."""
    if packs_dir is None:
        packs_dir = os.path.join(os.path.dirname(__file__))
    pack_dir = os.path.join(packs_dir, pack_id)

    if not os.path.isdir(pack_dir):
        raise PackLoadError(pack_id, [f"No pack folder found at {pack_dir}"])

    try:
        meta = _read_yaml(os.path.join(pack_dir, "pack.yaml"))
        questionnaire = _read_yaml(os.path.join(pack_dir, "questionnaire.yaml"))
        event_schemas = _read_yaml(os.path.join(pack_dir, "event_schemas.yaml"))
        prompts = _read_yaml(os.path.join(pack_dir, "prompts.yaml"))
        approval_defaults = _read_yaml(os.path.join(pack_dir, "approval_defaults.yaml"))
    except FileNotFoundError as e:
        raise PackLoadError(pack_id, [f"Missing required file: {e.filename}"])
    except yaml.YAMLError as e:
        raise PackLoadError(pack_id, [f"Invalid YAML syntax: {e}"])

    try:
        pack = Pack(
            id=meta.get("id"),
            version=meta.get("version"),
            display_name=meta.get("display_name"),
            questions=questionnaire.get("questions", []),
            event_schemas=event_schemas.get("schemas", []),
            prompts=prompts.get("templates", {}),
            approval_defaults=approval_defaults.get("defaults", {}),
        )
    except ValidationError as e:
        # Pydantic's own errors, one per malformed field, structural only.
        issues = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise PackLoadError(pack_id, issues)

    # Structural validation passed. Now check cross-file consistency.
    issues = validate_pack(pack)
    if issues:
        raise PackLoadError(pack_id, issues)

    return pack


def list_packs(packs_dir: str = None) -> list[dict]:
    """Scans packs/ for subfolders with a pack.yaml and returns their id +
    display_name, so callers (the frontend's pack picker) never have to
    hardcode which packs exist."""
    if packs_dir is None:
        packs_dir = os.path.join(os.path.dirname(__file__))

    packs = []
    for entry in sorted(os.listdir(packs_dir)):
        pack_dir = os.path.join(packs_dir, entry)
        pack_yaml = os.path.join(pack_dir, "pack.yaml")
        if os.path.isdir(pack_dir) and os.path.isfile(pack_yaml):
            meta = _read_yaml(pack_yaml)
            packs.append({"id": meta.get("id", entry), "display_name": meta.get("display_name", entry)})
    return packs


# ---------- cross-file validation ----------

_SETTINGS_REF = re.compile(r"\{\{\s*settings\.(\w+)\s*\}\}")


def validate_pack(pack: Pack) -> list[str]:
    """Checks the pack for internal inconsistencies that Pydantic can't see,
    since each check compares content across two different files.
    Returns a list of human-readable issues; empty list means the pack is clean."""
    issues = []

    known_settings_keys = {q.settings_key for q in pack.questions}
    known_action_types = {
        at for schema in pack.event_schemas for at in schema.valid_action_types
    }

    # 1. Every {{settings.x}} referenced in a prompt must have a matching question.
    for template_name, template_text in pack.prompts.items():
        for ref in _SETTINGS_REF.findall(template_text):
            if ref not in known_settings_keys:
                issues.append(
                    f"prompts.templates.{template_name} references settings.{ref}, "
                    f"but no question in questionnaire.yaml has settings_key: {ref}"
                )

    # 2. Every action_type an event schema can produce must have an approval default.
    for schema in pack.event_schemas:
        for action_type in schema.valid_action_types:
            if action_type not in pack.approval_defaults:
                issues.append(
                    f"event_schemas '{schema.event_type}' allows action_type '{action_type}', "
                    f"but approval_defaults.yaml has no default for it"
                )

    # 3. Every approval default should correspond to a real action_type somewhere
    #    (catches stale entries left behind after a schema changes).
    for action_type in pack.approval_defaults:
        if action_type not in known_action_types:
            issues.append(
                f"approval_defaults.yaml has a default for '{action_type}', "
                f"but no event schema declares it as a valid_action_type"
            )

    return issues


if __name__ == "__main__":
    pack = load_pack("realestate")
    print(f"Loaded pack: {pack.display_name} (v{pack.version})")
    print(f"  Questions: {[q.id for q in pack.questions]}")
    print(f"  Event schemas: {[s.event_type for s in pack.event_schemas]}")
    print(f"  Prompts: {list(pack.prompts.keys())}")
    print(f"  Approval defaults: {pack.approval_defaults}")
