"""
Demo spike: an agent proposes a Config Pack change from one plain-English
sentence. Validated by the real pack loader before anything is written.
Never touches the real pack until a human types "y".

    python scripts/propose_config_change.py healthclub "Add a preferred class time field to leads"

This is a spike, not a module other code imports -- it only reads pack files
and calls the existing call_llm(), the same one the event pipeline uses.
No existing module is modified to build this.
"""

import argparse
import difflib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operations_nerd"))

from dotenv import load_dotenv
load_dotenv()

from pipeline.llm_client import call_llm
from packs.loader import load_pack, PackLoadError

PACKS_DIR = os.path.join(os.path.dirname(__file__), "..", "operations_nerd", "packs")

# The only files this spike is allowed to propose changes to.
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


def build_prompt(pack_id: str, sentence: str, current_files: dict) -> str:
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


def load_current_files(pack_dir: str) -> dict:
    current = {}
    for filename in EDITABLE_FILES:
        path = os.path.join(pack_dir, filename)
        if os.path.isfile(path):
            with open(path, "r") as f:
                current[filename] = f.read()
        else:
            current[filename] = ""  # e.g. entity_schemas.yaml may not exist yet
    return current


def make_temp_pack_copy(pack_dir: str) -> str:
    tmp_root = tempfile.mkdtemp(prefix="pack_proposal_")
    tmp_pack_dir = os.path.join(tmp_root, os.path.basename(pack_dir))
    shutil.copytree(pack_dir, tmp_pack_dir)
    return tmp_root


def print_diff(filename: str, before: str, after: str):
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{filename}",
        tofile=f"b/{filename}",
    )
    diff_text = "".join(diff)
    print(f"\n--- Proposed diff for {filename} ---")
    print(diff_text if diff_text else "(no textual change)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_id")
    parser.add_argument("sentence")
    args = parser.parse_args()

    pack_dir = os.path.join(PACKS_DIR, args.pack_id)
    if not os.path.isdir(pack_dir):
        print(f"No pack folder found at {pack_dir}")
        sys.exit(1)

    current_files = load_current_files(pack_dir)

    print(f"Pack: {args.pack_id}")
    print(f"Request: {args.sentence}\n")
    print("Calling LLM to propose a change...")

    prompt = build_prompt(args.pack_id, args.sentence, current_files)
    raw = call_llm(prompt, response_schema=RESPONSE_SCHEMA)
    proposal = json.loads(raw)

    target_file = proposal["target_file"]
    new_content = proposal["new_content"]
    explanation = proposal["explanation"]

    print(f"\nAgent proposes changing: {target_file}")
    print(f"Reasoning: {explanation}")
    print_diff(target_file, current_files[target_file], new_content)

    # ---- Validate the proposal against a throwaway copy of the pack ----
    tmp_root = make_temp_pack_copy(pack_dir)
    try:
        tmp_pack_dir = os.path.join(tmp_root, args.pack_id)
        with open(os.path.join(tmp_pack_dir, target_file), "w") as f:
            f.write(new_content)

        try:
            load_pack(args.pack_id, packs_dir=tmp_root)
            print("\nValidation: PASS -- the pack loads cleanly with this change.")
            validation_passed = True
        except PackLoadError as e:
            print("\nValidation: FAIL")
            print(str(e))
            validation_passed = False
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    if not validation_passed:
        print("\nNot applying: validation failed.")
        return

    answer = input("\nApply? [y/N] ").strip().lower()
    if answer == "y":
        with open(os.path.join(pack_dir, target_file), "w") as f:
            f.write(new_content)
        print(f"Applied. {target_file} updated.")
    else:
        print("Not applied.")


if __name__ == "__main__":
    main()
