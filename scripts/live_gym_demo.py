"""
Live demo: one real chat turn against Anthropic that proposes a gym CRM
for an empty business. Prints the proposal; applies nothing.

    python scripts/live_gym_demo.py

Needs ANTHROPIC_API_KEY (and optionally ANTHROPIC_MODEL). Does nothing
without a key. Runs against a throwaway database, never the real one.
"""

import functools
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "operations_nerd"))

from dotenv import load_dotenv
load_dotenv()

MESSAGE = ("I run a gym called Iron Works with two locations. I sell monthly memberships "
           "(basic and premium), I want to keep track of people who ask about joining, "
           "and I want to know which classes we offer and where.")


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set; nothing to run.")
        return 1

    from db import db as d
    tmp = os.path.join(tempfile.mkdtemp(), "demo.db")
    d.init_db(tmp)
    d.get_conn = functools.partial(d.get_conn, tmp)      # everything below uses the throwaway DB

    from agentic import auth, chat, store
    from agentic.spec import empty_spec

    with d.get_conn() as conn:
        account_id = store.create_account(conn, "demo@example.com", auth.hash_password("demo-password"))
        business_id = store.create_business_with_version_1(
            conn, account_id, "Iron Works", None, empty_spec("Iron Works"))

    print(f"Owner: {MESSAGE}\n")
    proposal = chat.chat(account_id, business_id, [{"role": "user", "content": MESSAGE}])
    print("Agent:", proposal["reply"], "\n")
    print(json.dumps({k: proposal[k] for k in ("status", "operations", "rejected_operations", "impact")}, indent=2))
    with d.get_conn() as conn:
        row = conn.execute("SELECT model, input_tokens, output_tokens, latency_ms FROM proposals").fetchone()
    print("\nAudit:", dict(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
