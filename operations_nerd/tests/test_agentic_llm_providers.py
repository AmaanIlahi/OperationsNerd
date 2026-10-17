"""
Provider selection, the Gemini provider, and the agent turn's one retry.

No network: the Gemini SDK client is mocked and the agent turn runs against
a stub (agentic.chat.run_llm). Run from operations_nerd/:
    python tests/test_agentic_llm_providers.py
"""

import json
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["LLM_PROVIDER"] = "stub"      # tests never reach a real model

from db import db as d
from agentic import auth, chat, config, gemini_llm, llm, store
from agentic.llm import LLMError, LLMResult
from agentic.spec import empty_spec

d.init_db(reset=True)


@contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def new_business():
    with d.get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        account = store.create_account(conn, f"p{n}@example.com", auth.hash_password("correct horse battery"))
        biz = store.create_business_with_version_1(conn, account, "Biz", None, empty_spec("Biz"))
    return account, biz


def scripted(*texts):
    """A run_llm that returns the given texts in order (the last one repeats)."""
    calls = []

    def fn(system, messages, schema):
        calls.append(messages)
        return LLMResult(texts[min(len(calls), len(texts)) - 1], "m", 10, 5, 7)
    fn.calls = calls
    return fn


def ok(ops=None):
    return json.dumps({"reply": "done", "operations": ops or []})


GOOD_OP = {"op": "create_entity_type", "ref": "member", "label": "Member"}
BAD_OP = {"op": "add_field", "entity": "crm_nope", "label": "X", "type": "text"}


def run_chat(account, biz, run_llm, text="go"):
    original, chat.run_llm = chat.run_llm, run_llm
    try:
        return chat.chat(account, biz, [{"role": "user", "content": text}])
    finally:
        chat.run_llm = original


# ---------- provider selection ----------

def test_default_provider_is_gemini_and_invalid_is_rejected():
    with env(LLM_PROVIDER=None):
        assert llm.get_provider() == "gemini" and llm.required_key_env() == "GEMINI_API_KEY"
    with env(LLM_PROVIDER="Anthropic"):
        assert llm.get_provider() == "anthropic" and llm.required_key_env() == "ANTHROPIC_API_KEY"
    with env(LLM_PROVIDER="stub"):
        assert llm.required_key_env() is None
        res = llm.call_structured("s", [{"role": "user", "content": "hi"}], {})
        assert json.loads(res.text)["operations"] == [] and res.model == "stub"
    with env(LLM_PROVIDER="openai"):
        try:
            llm.get_provider()
        except LLMError as e:
            assert "gemini, anthropic, stub" in str(e)
        else:
            raise AssertionError("unknown provider accepted")


def test_anthropic_types_are_the_shared_ones():
    from agentic import anthropic_llm
    assert anthropic_llm.LLMResult is LLMResult and anthropic_llm.LLMError is LLMError


def test_dispatcher_routes_to_selected_provider():
    with env(LLM_PROVIDER="gemini"), patch("agentic.gemini_llm.call_structured", return_value="G") as g:
        assert llm.call_structured("s", [], {}) == "G" and g.called
    with env(LLM_PROVIDER="anthropic"), patch("agentic.anthropic_llm.call_structured", return_value="A") as a:
        assert llm.call_structured("s", [], {}) == "A" and a.called


def test_production_startup_checks_follow_the_provider():
    base = dict(APP_ENV="production", SESSION_SECRET="s" * 40, CONFIG_AGENT_ENABLED=None)
    with env(**base, LLM_PROVIDER=None, GEMINI_API_KEY="k", ANTHROPIC_API_KEY=None):
        assert config.check_environment() == []
    with env(**base, LLM_PROVIDER=None, GEMINI_API_KEY=None, ANTHROPIC_API_KEY="k"):
        assert any("GEMINI_API_KEY is required" in p for p in config.check_environment())
    with env(**base, LLM_PROVIDER="anthropic", GEMINI_API_KEY=None, ANTHROPIC_API_KEY="k"):
        assert config.check_environment() == []
    with env(**base, LLM_PROVIDER="anthropic", GEMINI_API_KEY="k", ANTHROPIC_API_KEY=None):
        assert any("ANTHROPIC_API_KEY is required" in p for p in config.check_environment())
    with env(**base, LLM_PROVIDER="bogus", GEMINI_API_KEY="k"):
        assert any("LLM_PROVIDER must be one of" in p for p in config.check_environment())


# ---------- Gemini provider (SDK mocked) ----------

def fake_client(response=None, error=None, seen=None):
    class Models:
        def generate_content(self, model, contents, config):
            if seen is not None:
                seen.update(model=model, contents=contents, config=config)
            if error:
                raise error
            return response

    class Client:
        def __init__(self, api_key=None, http_options=None):
            if seen is not None:
                seen["api_key"] = api_key
            self.models = Models()
    return Client


def test_gemini_request_and_parsing():
    seen = {}
    usage = SimpleNamespace(prompt_token_count=40, candidates_token_count=9, thoughts_token_count=3)
    response = SimpleNamespace(text=ok(), usage_metadata=usage, model_version="gemini-3.8-flash")
    with env(GEMINI_API_KEY="test-key", GEMINI_MODEL=None):
        with patch("google.genai.Client", fake_client(response, seen=seen)):
            res = gemini_llm.call_structured(
                "sys", [{"role": "user", "content": "yo"}, {"role": "assistant", "content": "hey"},
                        {"role": "user", "content": "again"}], chat.RESPONSE_SCHEMA)
    assert json.loads(res.text) == {"reply": "done", "operations": []}
    assert (res.model, res.input_tokens, res.output_tokens) == ("gemini-3.8-flash", 40, 12)
    assert seen["api_key"] == "test-key" and seen["model"] == "gemini-3.8-flash"
    assert [c.role for c in seen["contents"]] == ["user", "model", "user"]
    assert seen["config"].system_instruction == "sys"
    assert seen["config"].response_mime_type == "application/json"
    sent = seen["config"].response_json_schema            # operations go out as one exact object per operation
    assert sent["required"] == ["reply", "operations"]
    variants = sent["properties"]["operations"]["items"]["anyOf"]
    assert len(variants) == len(chat.OPERATION_NAMES)
    assert [v["properties"]["op"]["const"] for v in variants] == chat.OPERATION_NAMES
    add_field = next(v for v in variants if v["properties"]["op"]["const"] == "add_field")
    assert set(add_field["required"]) == {"op", "entity", "label", "type"} and "cardinality" not in add_field["properties"]
    add_link = next(v for v in variants if v["properties"]["op"]["const"] == "add_link")
    assert "from" in add_link["properties"] and "from_" not in add_link["properties"]
    assert not {"title", "additionalProperties", "pattern", "$defs"} & set(json.dumps(sent).replace('"', " ").split())
    assert gemini_llm.typed_schema({"type": "object", "properties": {"text": {"type": "string"}}})         == {"type": "object", "properties": {"text": {"type": "string"}}}


def test_gemini_model_comes_from_env():
    seen = {}
    response = SimpleNamespace(text=ok(), usage_metadata=None, model_version=None)
    with env(GEMINI_API_KEY="k", GEMINI_MODEL="gemini-x"):
        with patch("google.genai.Client", fake_client(response, seen=seen)):
            res = gemini_llm.call_structured("s", [{"role": "user", "content": "q"}], {})
    assert seen["model"] == "gemini-x" and res.model == "gemini-x" and res.input_tokens is None


def test_gemini_errors_are_llm_errors_and_leak_nothing():
    with env(GEMINI_API_KEY=None):
        try:
            gemini_llm.call_structured("s", [], {})
        except LLMError as e:
            assert "GEMINI_API_KEY" in str(e)
        else:
            raise AssertionError("missing key accepted")
    with env(GEMINI_API_KEY="secret-key-123"):
        with patch("google.genai.Client", fake_client(error=RuntimeError("boom secret-key-123"))):
            try:
                gemini_llm.call_structured("s", [{"role": "user", "content": "q"}], {})
            except LLMError as e:
                assert "secret-key-123" not in str(e) and "RuntimeError" in str(e)
            else:
                raise AssertionError("provider failure not reported")
        empty = SimpleNamespace(text="", usage_metadata=None, model_version=None)
        with patch("google.genai.Client", fake_client(empty)):
            try:
                gemini_llm.call_structured("s", [{"role": "user", "content": "q"}], {})
            except LLMError:
                pass
            else:
                raise AssertionError("empty answer accepted")


# ---------- the agent turn's one retry ----------

def test_bad_json_is_retried_once_with_the_problem_included():
    account, biz = new_business()
    stub = scripted("not json {", ok([GOOD_OP]))
    view = run_chat(account, biz, stub)
    assert len(stub.calls) == 2 and view["status"] == "pending" and len(view["operations"]) == 1
    retry = stub.calls[1]
    assert retry[-2] == {"role": "assistant", "content": "not json {"}
    assert retry[-1]["role"] == "user" and "could not be fully used" in retry[-1]["content"]
    with d.get_conn() as conn:
        row = conn.execute("SELECT input_tokens, output_tokens, latency_ms FROM proposals WHERE id=?",
                           (view["proposal_id"],)).fetchone()
    assert tuple(row) == (20, 10, 14)          # both calls are in the audit trail


def test_still_unreadable_after_retry_is_a_clear_error_not_a_crash():
    account, biz = new_business()
    stub = scripted("nope", "still nope")
    try:
        run_chat(account, biz, stub)
    except chat.ChangeError as e:
        assert e.status == 502 and "unreadable" in str(e.detail)
    else:
        raise AssertionError("garbage accepted")
    assert len(stub.calls) == 2


def test_all_operations_refused_is_retried_with_the_reasons():
    account, biz = new_business()
    stub = scripted(ok([BAD_OP]), ok([GOOD_OP]))
    view = run_chat(account, biz, stub)
    assert len(stub.calls) == 2 and view["status"] == "pending" and view["rejected_operations"] == []
    assert "refused by the server" in stub.calls[1][-1]["content"] and "crm_nope" in stub.calls[1][-1]["content"]


def test_refused_twice_shows_the_refusals_and_applies_nothing():
    account, biz = new_business()
    stub = scripted(ok([BAD_OP]))
    view = run_chat(account, biz, stub)
    assert len(stub.calls) == 2 and view["status"] == "no_changes" and len(view["rejected_operations"]) == 1


def test_fully_valid_answers_are_not_retried():
    account, biz = new_business()
    for ops in ([], [GOOD_OP]):
        stub = scripted(ok(ops))
        run_chat(account, biz, stub)
        assert len(stub.calls) == 1, ops


def test_partly_refused_answer_is_retried_once():
    account, biz = new_business()
    stub = scripted(ok([GOOD_OP, BAD_OP]), ok([GOOD_OP]))
    view = run_chat(account, biz, stub)
    assert len(stub.calls) == 2 and view["rejected_operations"] == [] and len(view["operations"]) == 1
    feedback = stub.calls[1][-1]["content"]
    assert "1 operation(s) were refused" in feedback and "crm_nope" in feedback
    assert "expected shape -> add_field: entity, label, type" in feedback           # the exact shape of the refused op


REF_STYLE = ok([
    {"op": "create_entity_type", "label": "Owner", "ref": "owner"},
    {"op": "create_entity_type", "label": "Dog", "ref": "dog"},
    {"op": "add_field", "entity": "ref:owner", "label": "Name", "type": "text"},
    {"op": "add_field", "entity": "ref:dog", "label": "Name", "type": "text"},
    {"op": "add_link", "label": "Owner", "from": "ref:dog", "to": "ref:owner", "cardinality": "many_to_one"}])
CORRECT = ok([
    {"op": "create_entity_type", "label": "Owner", "ref": "owner"},
    {"op": "create_entity_type", "label": "Dog", "ref": "dog"},
    {"op": "add_field", "entity": "owner", "label": "Name", "type": "text"},
    {"op": "add_field", "entity": "dog", "label": "Name", "type": "text"},
    {"op": "add_link", "label": "Owner", "from": "dog", "to": "owner", "cardinality": "many_to_one"}])


def test_ref_style_proposal_is_corrected_on_retry_and_the_correct_one_is_applied():
    account, biz = new_business()
    stub = scripted(REF_STYLE, CORRECT)
    view = run_chat(account, biz, stub, "I run a dog grooming shop")
    assert len(stub.calls) == 2
    assert view["rejected_operations"] == [] and len(view["operations"]) == 5
    feedback = stub.calls[1][-1]["content"]
    assert "ref:owner" in feedback and "does not exist" in feedback and "add_link: label, from, to" in feedback
    out = chat.approve(account, view["proposal_id"])                               # and it really applies
    spec = out["spec"]
    owner = next(e for e in spec["entities"] if e["label"] == "Owner")
    dog = next(e for e in spec["entities"] if e["label"] == "Dog")
    assert [f["label"] for f in owner["fields"]] == ["Name"] and [f["label"] for f in dog["fields"]] == ["Name"]
    (link,) = spec["links"]
    assert (link["from"], link["to"]) == (dog["key"], owner["key"])
    with d.get_conn() as conn:
        row = conn.execute("SELECT input_tokens, output_tokens, latency_ms FROM proposals WHERE id=?",
                           (view["proposal_id"],)).fetchone()
    assert tuple(row) == (20, 10, 14)                                              # both calls, one daily turn


def test_the_answer_with_fewer_refusals_is_kept_and_a_tie_goes_to_the_retry():
    account, biz = new_business()
    worse = ok([GOOD_OP, BAD_OP, BAD_OP])                                          # 2 refusals
    better = ok([GOOD_OP, BAD_OP])                                                 # 1 refusal
    # retry is worse than the first answer: keep the first
    view = run_chat(account, biz, scripted(better, worse))
    assert len(view["rejected_operations"]) == 1
    # retry is better: keep the retry
    view = run_chat(account, biz, scripted(worse, better))
    assert len(view["rejected_operations"]) == 1
    # tie: the retry wins (it is the one that saw the feedback)
    first_tie = ok([GOOD_OP, BAD_OP])
    retry_tie = json.dumps({"reply": "retry", "operations": [GOOD_OP, BAD_OP]})
    view = run_chat(account, biz, scripted(first_tie, retry_tie))
    assert view["reply"] == "retry"


def test_unreadable_retry_keeps_a_readable_first_answer():
    account, biz = new_business()
    view = run_chat(account, biz, scripted(ok([GOOD_OP, BAD_OP]), "garbage {"))
    assert view["status"] == "pending" and len(view["operations"]) == 1 and len(view["rejected_operations"]) == 1


def test_system_prompt_has_the_correct_example_and_the_no_ref_prefix_rule():
    from agentic.spec import empty_spec
    prompt = chat.build_system_prompt(empty_spec("x"), None, [])
    assert 'add_link            label "Owner", from "dog", to "owner"' in prompt
    assert '(no "ref:")' in prompt and "the target goes in `to`" in prompt


def test_retry_does_not_spend_a_second_daily_turn():
    account, biz = new_business()

    def used():
        with d.get_conn() as conn:
            return conn.execute("SELECT COALESCE(SUM(turns),0) FROM chat_usage WHERE account_id=?",
                                (account,)).fetchone()[0]
    before = used()
    run_chat(account, biz, scripted("bad", ok()))
    assert used() - before == 1


def test_provider_outage_on_retry_refunds_the_turn():
    account, biz = new_business()
    n = [0]

    def flaky(system, messages, schema):
        n[0] += 1
        if n[0] == 1:
            return LLMResult("bad", "m", 1, 1, 1)
        raise LLMError("HTTP 503")
    try:
        run_chat(account, biz, flaky)
    except chat.ChangeError as e:
        assert e.status == 502 and "unavailable" in str(e.detail)
    else:
        raise AssertionError("outage swallowed")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"PASS {name}")
    print(f"\n{len(tests)} tests passed")
