"""LLM provider layer: offline mock, provider selection, retry on transient errors. No network."""

import sys
import os
import json
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline import llm_client as lc

os.environ["LLM_PROVIDER"] = "mock"

print("Testing mock provider...")
schema = {"type": "object", "properties": {"sender": {"type": "string"}, "action_type": {"type": "string", "enum": ["reply_inquiry", "schedule_showing"]}}}
out = json.loads(lc.call_llm("Customer wants to schedule showing on Friday", response_schema=schema))
assert out["action_type"] == "schedule_showing", out
assert out["sender"] == "mock sender"
assert json.loads(lc.call_llm("hello", response_schema=schema))["action_type"] == "reply_inquiry"
assert lc.call_llm("Say hi").startswith("Thanks")
print("  OK")

print("Testing unknown provider...")
os.environ["LLM_PROVIDER"] = "nope"
try:
    lc.call_llm("x")
    raise SystemExit("should have raised")
except RuntimeError as e:
    assert "Unknown LLM_PROVIDER" in str(e)
print("  OK")

print("Testing retry on transient errors...")
calls = {"n": 0}
def flaky(prompt, schema):
    calls["n"] += 1
    if calls["n"] < 3:
        raise lc.LLMError("HTTP 429 from model server")
    return "ok"
os.environ["LLM_PROVIDER"] = "mock"
with patch.dict(lc.PROVIDERS, {"mock": flaky}), patch.object(lc.time, "sleep", lambda s: None):
    assert lc.call_llm("x") == "ok" and calls["n"] == 3
calls["n"] = 0
def hard(prompt, schema):
    calls["n"] += 1
    raise lc.LLMError("HTTP 401 from model server")
with patch.dict(lc.PROVIDERS, {"mock": hard}), patch.object(lc.time, "sleep", lambda s: None):
    try:
        lc.call_llm("x")
        raise SystemExit("should have raised")
    except lc.LLMError:
        assert calls["n"] == 1
print("  OK")

print("Testing openai request shape...")
os.environ["LLM_PROVIDER"] = "openai"
os.environ["OPENAI_API_KEY"] = "k"
seen = {}
def fake_post(url, payload, headers, timeout=120):
    seen.update(url=url, payload=payload, headers=headers)
    return {"choices": [{"message": {"content": "{\"a\": \"b\"}"}}]}
with patch.object(lc, "_post_json", fake_post):
    assert lc.call_llm("q", response_schema={"type": "object", "properties": {"a": {"type": "string"}}}) == "{\"a\": \"b\"}"
assert seen["url"].endswith("/chat/completions") and seen["headers"]["Authorization"] == "Bearer k"
assert seen["payload"]["response_format"] == {"type": "json_object"}
print("  OK")

print("LLM provider smoke test passed.")
