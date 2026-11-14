"""Offline Baseten transport, structured-output and credential-boundary checks."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic import baseten_llm as provider, chat, llm


SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"},
          "operations": {"type": "array", "items": {"type": "object"}}},
          "required": ["reply", "operations"]}


class BasetenProviderTests(unittest.TestCase):
    def setUp(self):
        self.settings = patch.dict(os.environ, {"LLM_PROVIDER": "baseten", "BASETEN_API_KEY": "fictional-provider-key",
            "BASETEN_MODEL": "openai/gpt-oss-120b", "BASETEN_BASE_URL": "https://inference.baseten.co/v1"})
        self.settings.start()

    def tearDown(self):
        self.settings.stop()

    def call(self, handler, schema=SCHEMA):
        original = httpx.Client
        transport = httpx.MockTransport(handler)
        def client(**kwargs):
            self.assertFalse(kwargs["follow_redirects"])
            return original(transport=transport, **kwargs)
        with patch.object(provider.httpx, "Client", side_effect=client):
            return llm.call_structured("Fictional system", [{"role": "user", "content": "Fictional input"}], schema)

    def good(self, request):
        self.assertEqual(request.url, "https://inference.baseten.co/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer fictional-provider-key")
        payload = json.loads(request.content)
        self.assertEqual(payload["model"], "openai/gpt-oss-120b")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        return httpx.Response(200, json={"model": "openai/gpt-oss-120b", "choices": [{"finish_reason": "stop",
            "message": {"content": '{"reply":"Review first","operations":[]}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8}})

    def test_dispatch_and_exact_structured_transport(self):
        result = self.call(self.good)
        self.assertEqual(result.model, "openai/gpt-oss-120b")
        self.assertEqual((result.input_tokens, result.output_tokens), (12, 8))
        self.assertEqual(json.loads(result.text)["operations"], [])
        self.assertEqual(llm.required_key_env(), "BASETEN_API_KEY")

    def test_real_operation_shapes_are_supplied_without_mutating_core_schema(self):
        before = json.dumps(chat.RESPONSE_SCHEMA, sort_keys=True)
        result = provider.typed_schema(chat.RESPONSE_SCHEMA)
        variants = result["properties"]["operations"]["items"]["anyOf"]
        self.assertGreater(len(variants), 5)
        create = next(value for value in variants if value["properties"]["op"].get("const") == "create_entity_type")
        self.assertIn("label", create["required"])
        self.assertEqual(create["additionalProperties"], False)
        self.assertEqual(json.dumps(chat.RESPONSE_SCHEMA, sort_keys=True), before)

    def test_missing_key_never_opens_a_client(self):
        with patch.dict(os.environ, {"BASETEN_API_KEY": ""}), patch.object(provider.httpx, "Client") as client:
            with self.assertRaisesRegex(llm.LLMError, "BASETEN_API_KEY"):
                provider.call_structured("x", [], SCHEMA)
            client.assert_not_called()

    def test_untrusted_endpoint_and_bad_model_are_rejected_before_transport(self):
        for url in ("http://inference.baseten.co/v1", "https://example.com/v1",
                    "https://inference.baseten.co/v1?secret=value", "https://name:secret@inference.baseten.co/v1",
                    "https://inference.baseten.co:444/v1"):
            with patch.dict(os.environ, {"BASETEN_BASE_URL": url}), patch.object(provider.httpx, "Client") as client:
                with self.assertRaises(llm.LLMError):
                    provider.call_structured("x", [], SCHEMA)
                client.assert_not_called()
        with patch.dict(os.environ, {"BASETEN_MODEL": "invalid model\nheader"}):
            with self.assertRaises(llm.LLMError):
                provider.get_model()

    def test_error_bodies_and_request_secrets_are_not_exposed(self):
        with self.assertRaises(llm.LLMError) as caught:
            self.call(lambda request: httpx.Response(401, text="private-business-data fictional-provider-key"))
        self.assertEqual(str(caught.exception), "Baseten returned HTTP 401")

    def test_truncated_refused_and_malformed_answers_fail_safely(self):
        answers = [
            {"choices": [{"finish_reason": "length", "message": {"content": '{}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"refusal": "private", "content": '{}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": 'not json'}}]},
            {"choices": []},
            {"choices": [{"finish_reason": "stop", "message": {"content": '[]'}}]},
        ]
        for answer in answers:
            with self.assertRaises(llm.LLMError):
                self.call(lambda request: httpx.Response(200, json=answer))

    def test_bounded_response_and_transport_errors(self):
        with self.assertRaisesRegex(llm.LLMError, "size limit"):
            self.call(lambda request: httpx.Response(200, content=b"x" * (provider.MAX_RESPONSE_BYTES + 1)))
        def timeout(request):
            raise httpx.ReadTimeout("private-business-data fictional-provider-key", request=request)
        with self.assertRaises(llm.LLMError) as caught:
            self.call(timeout)
        self.assertEqual(str(caught.exception), "Could not reach Baseten (ReadTimeout)")


if __name__ == "__main__":
    unittest.main()
