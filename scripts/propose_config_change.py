"""
Demo spike: an agent proposes a Config Pack change from one plain-English
sentence. Validated by the real pack loader before anything is written.
Never touches the real pack until a human types "y".

    python scripts/propose_config_change.py healthclub "Add a preferred class time field to leads"

CLI wrapper around operations_nerd/config_agent/service.py, which also backs
the /config-agent HTTP endpoints used by the frontend_agent browser demo --
both share the same propose/validate/apply implementation. No existing
module is modified to build this.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operations_nerd"))

from dotenv import load_dotenv
load_dotenv()

from config_agent.service import propose, apply, ConfigAgentError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack_id")
    parser.add_argument("sentence")
    args = parser.parse_args()

    print(f"Pack: {args.pack_id}")
    print(f"Request: {args.sentence}\n")
    print("Calling LLM to propose a change...")

    try:
        result = propose(args.pack_id, args.sentence)
    except ConfigAgentError as e:
        print(f"\n{e}")
        sys.exit(1)

    print(f"\nAgent proposes changing: {result['target_file']}")
    print(f"Reasoning: {result['explanation']}")
    print(f"\n--- Proposed diff for {result['target_file']} ---")
    print(result["diff"] if result["diff"] else "(no textual change)")

    if result["validation"] == "pass":
        print("\nValidation: PASS -- the pack loads cleanly with this change.")
    else:
        print("\nValidation: FAIL")
        for issue in result["errors"]:
            print(f"  - {issue}")
        print("\nNot applying: validation failed.")
        return

    answer = input("\nApply? [y/N] ").strip().lower()
    if answer == "y":
        apply(args.pack_id, result["target_file"], result["new_content"], result["base_hash"])
        print(f"Applied. {result['target_file']} updated.")
    else:
        print("Not applied.")


if __name__ == "__main__":
    main()
