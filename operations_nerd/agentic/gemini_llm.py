"""
Gemini provider for the agentic CRM (google-genai SDK).

Same call shape as anthropic_llm.call_structured: system prompt + chat
messages + JSON schema in, JSON text + usage out. Structured output uses the
API's JSON mode with the response schema, so the model can only answer in the
{reply, operations} shape; the result is parsed and validated by the caller
exactly as for any provider. Nothing the model says is ever executed.

Env vars: GEMINI_API_KEY, GEMINI_MODEL.
"""

import json
import os
import time

from agentic.llm import LLMError, LLMResult

# Exact ID taken from client.models.list() for this project's key.
DEFAULT_MODEL = "gemini-3.8-flash"
MAX_OUTPUT_TOKENS = 8192      # headroom: thinking tokens count against this
TIMEOUT_MS = 90_000


_UNSUPPORTED = {"title", "default", "additionalProperties", "discriminator", "pattern"}


def _clean(node):
    """A pydantic JSON schema reduced to what Gemini's JSON mode accepts."""
    if isinstance(node, dict):
        if "anyOf" in node:                                   # Optional[X] -> X
            non_null = [x for x in node["anyOf"] if x.get("type") != "null"]
            if len(non_null) == 1:
                node = {**{k: v for k, v in node.items() if k != "anyOf"}, **non_null[0]}
        return {k: _clean(v) for k, v in node.items() if k not in _UNSUPPORTED}
    if isinstance(node, list):
        return [_clean(x) for x in node]
    return node


def typed_schema(response_schema: dict) -> dict:
    """Gemini fills a flat all-optional operation object sparsely (labels
    missing, attributes from other operations mixed in). So the operations list
    is given as one exact object per operation, built from the same pydantic
    models the validator uses. The validator is unchanged; this only makes the
    model's output fit it. Any other schema passes through untouched."""
    items = (response_schema.get("properties", {}).get("operations", {}).get("items", {}))
    if "op" not in items.get("properties", {}):
        return response_schema
    from agentic.operations import Operation
    variants = [_clean(m.model_json_schema(by_alias=True)) for m in Operation.__args__[0].__args__]
    typed = json.loads(json.dumps(response_schema))
    typed["properties"]["operations"]["items"] = {"anyOf": variants}
    return typed


def get_model() -> str:
    return os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL


def _contents(types, messages: list[dict]):
    return [
        types.Content(role="model" if m["role"] == "assistant" else "user",
                      parts=[types.Part(text=m["content"])])
        for m in messages
    ]


def call_structured(system: str, messages: list[dict], response_schema: dict) -> LLMResult:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError("GEMINI_API_KEY is not set")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise LLMError("The google-genai package is not installed")
    model = get_model()
    started = time.monotonic()
    try:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=TIMEOUT_MS))
        resp = client.models.generate_content(
            model=model,
            contents=_contents(types, messages),
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_json_schema=typed_schema(response_schema),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
    except Exception as e:      # SDK raises several error types; never leak details or the key
        code = getattr(e, "code", None)
        raise LLMError(f"Model provider error ({type(e).__name__}{f' {code}' if code else ''})")
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        text = resp.text
    except Exception:
        text = None
    if not text:
        raise LLMError("Model did not return a structured answer")
    usage = resp.usage_metadata
    out = None
    if usage is not None and usage.candidates_token_count is not None:
        out = usage.candidates_token_count + (usage.thoughts_token_count or 0)
    return LLMResult(
        text=text,
        model=getattr(resp, "model_version", None) or model,
        input_tokens=usage.prompt_token_count if usage is not None else None,
        output_tokens=out,
        latency_ms=latency_ms,
    )


def call_llm(prompt: str, response_schema=None) -> str:
    """Same contract as pipeline.llm_client.call_llm, backed by Gemini."""
    if response_schema is None:
        response_schema = {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}
    schema = (response_schema.model_json_schema()
              if hasattr(response_schema, "model_json_schema") else response_schema)
    return call_structured("Answer the user's request.", [{"role": "user", "content": prompt}], schema).text
