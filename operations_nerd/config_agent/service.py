"""
Config-change agent: propose a Config Pack file edit from one plain-English
sentence, validate it against a throwaway copy of the pack, apply only when
asked to. Shared by scripts/propose_config_change.py (CLI) and
config_agent/routes.py (the /config-agent HTTP endpoints used by the
frontend_agent demo page) so both go through one implementation.

Same pattern as the event pipeline: call_llm() constrained to a JSON schema,
caller parses and validates the result. Nothing here is vertical-specific --
the pack's own files are the only input, same as everywhere else in this app.
"""

import difflib
import hashlib
import json
import os
import shutil
import tempfile

from pipeline.llm_client import call_llm
from packs.loader import load_pack, PackLoadError

PACKS_DIR = os.path.join(os.path.dirname(__file__), "..", "packs")

# The only files this agent is allowed to propose changes to.
EDITABLE_FILES = ["entity_schemas.yaml", "questionnaire.yaml"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "target_file": {"type": "string", "enum": EDITABLE_FILES},
        "new_content": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["target_file", "new_content", "explanation"],
}


class ConfigAgentError(Exception):
    """Raised for problems that mean a proposal can't be produced at all
    (bad pack_id, unreadable pack files) -- distinct from a validation
    failure, which is a normal, expected outcome of a proposal."""


def pack_dir_for(pack_id: str) -> str:
    return os.path.join(PACKS_DIR, pack_id)


def _build_prompt(pack_id: str, sentence: str, current_files: dict) -> str:
    files_block = "\n\n".join(
        f"--- {name} (current content) ---\n{content}"
        for name, content in current_files.items()
    )
    return f"""You are proposing a change to the Config Pack "{pack_id}" for an
Operations Nerd business, based on one plain-English request from the pack
owner.

{files_block}

Owner's request: "{sentence}"

Decide which ONE file needs to change to satisfy this request, and return
the FULL new content of that file (not a diff, not a snippet -- the entire
file as it should read after the change). Keep everything else in the file
exactly as it was; only add or adjust what the request needs.

Rules for entity_schemas.yaml, if you choose it:
- Every entity_type must start with "crm_".
- "required" fields must also appear in "fields".
- Keep the existing entities and their fields; only add what's needed.

Rules for questionnaire.yaml, if you choose it:
- Each question needs: id, label, type, settings_key.
- type must be one of: text, number, boolean, select, multiselect.
- select/multiselect need an "options" list.

Return valid YAML content for new_content. Do not use markdown code fences."""


def load_current_files(pack_id: str) -> dict:
    """Reads the current content of every editable file in the pack.
    A missing file (e.g. entity_schemas.yaml not created yet) reads as ""."""
    pack_dir = pack_dir_for(pack_id)
    if not os.path.isdir(pack_dir):
        raise ConfigAgentError(f"No pack folder found at {pack_dir}")
    current = {}
    for filename in EDITABLE_FILES:
        path = os.path.join(pack_dir, filename)
        if os.path.isfile(path):
            with open(path, "r") as f:
                current[filename] = f.read()
        else:
            current[filename] = ""
    return current


def file_hash(content: str) -> str:
    """Fingerprint of a file's current content, handed to the client with a
    proposal and checked again on apply so a proposal can't be applied on
    top of a file that changed underneath it."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_diff(filename: str, before: str, after: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    return "".join(diff)


def propose(pack_id: str, sentence: str) -> dict:
    """Calls the LLM for one proposed file change and validates it against a
    throwaway copy of the pack. Writes nothing to the real pack. Returns:
    {target_file, new_content, explanation, diff, base_hash,
     validation: "pass" | "fail", errors: [str]}"""
    current_files = load_current_files(pack_id)

    prompt = _build_prompt(pack_id, sentence, current_files)
    raw = call_llm(prompt, response_schema=RESPONSE_SCHEMA)
    proposal = json.loads(raw)

    target_file = proposal["target_file"]
    new_content = proposal["new_content"]
    explanation = proposal["explanation"]
    before = current_files[target_file]

    validation, errors = validate_proposed_file(pack_id, target_file, new_content)

    return {
        "target_file": target_file,
        "new_content": new_content,
        "explanation": explanation,
        "diff": build_diff(target_file, before, new_content),
        "base_hash": file_hash(before),
        "validation": validation,
        "errors": errors,
    }


def validate_proposed_file(pack_id: str, target_file: str, new_content: str) -> tuple[str, list[str]]:
    """Writes new_content into a temp copy of the pack and runs the real
    pack loader against it. Never touches the real pack. Returns
    ("pass", []) or ("fail", [issue, ...])."""
    pack_dir = pack_dir_for(pack_id)
    tmp_root = tempfile.mkdtemp(prefix="pack_proposal_")
    try:
        tmp_pack_dir = os.path.join(tmp_root, pack_id)
        shutil.copytree(pack_dir, tmp_pack_dir)
        with open(os.path.join(tmp_pack_dir, target_file), "w") as f:
            f.write(new_content)
        try:
            load_pack(pack_id, packs_dir=tmp_root)
            return "pass", []
        except PackLoadError as e:
            return "fail", e.issues
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def apply(pack_id: str, target_file: str, new_content: str, base_hash: str) -> None:
    """Re-validates (never trust a caller-supplied validation result) and
    re-checks base_hash against the file's current content (never trust
    that nothing changed since propose()) before writing. Raises
    ConfigAgentError if either check fails; only writes on success."""
    if target_file not in EDITABLE_FILES:
        raise ConfigAgentError(f"'{target_file}' is not an editable pack file")

    current_files = load_current_files(pack_id)
    current_content = current_files[target_file]
    if file_hash(current_content) != base_hash:
        raise ConfigAgentError(
            f"{target_file} changed since this proposal was made; reload and try again"
        )

    validation, errors = validate_proposed_file(pack_id, target_file, new_content)
    if validation != "pass":
        raise ConfigAgentError("Validation failed: " + "; ".join(errors))

    pack_dir = pack_dir_for(pack_id)
    with open(os.path.join(pack_dir, target_file), "w") as f:
        f.write(new_content)
