"""
Live demo: one real chat turn against the selected LLM provider that proposes a gym CRM
for an empty business. Prints the proposal; applies nothing.

    python scripts/live_gym_demo.py

Provider comes from LLM_PROVIDER (gemini by default, or anthropic). Needs
that provider's key: GEMINI_API_KEY (optionally GEMINI_MODEL) or
ANTHROPIC_API_KEY (optionally ANTHROPIC_MODEL). Does nothing without a key,
and refuses the stub provider. Runs against a throwaway database, never the
real one.
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
    from agentic import llm
    try:
        provider = llm.get_provider()
    except llm.LLMError as e:
        print(e)
        return 1
    key_env = llm.required_key_env(provider)
    if key_env is None:
        print("LLM_PROVIDER=stub has no model to call; use gemini or anthropic.")
        return 1
    if not os.environ.get(key_env):
        print(f"{key_env} is not set; nothing to run.")
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

    print(f"Provider: {provider}\nOwner: {MESSAGE}\n")
    proposal = chat.chat(account_id, business_id, [{"role": "user", "content": MESSAGE}])
    print("Agent:", proposal["reply"], "\n")
    print(json.dumps({k: proposal[k] for k in ("status", "operations", "rejected_operations", "impact")}, indent=2))
    with d.get_conn() as conn:
        row = conn.execute("SELECT model, input_tokens, output_tokens, latency_ms FROM proposals").fetchone()
    print("\nAudit:", dict(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
