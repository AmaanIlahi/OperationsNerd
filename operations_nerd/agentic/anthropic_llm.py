"""
Anthropic provider for the agentic CRM.

Lives in agentic/ (not pipeline/llm_client.py) because existing modules must
stay untouched; it mirrors that module's call_llm(prompt, response_schema)
contract -- schema in, JSON text out -- so the provider stays swappable, and
adds call_structured() for what the agent turn needs on top: a system
prompt, real chat messages, and token usage for the audit trail.

Structured output uses a forced tool call: the model can only answer by
filling in the tool's input schema. The result is returned as JSON text;
nothing the model says is ever executed, only parsed by the caller.

Env vars: ANTHROPIC_API_KEY, ANTHROPIC_MODEL.
"""

import json
import os
import time
from dataclasses import dataclass

import httpx

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
TOOL_NAME = "submit_response"
MAX_OUTPUT_TOKENS = 4096
TIMEOUT_SECONDS = 90


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str                 # JSON text matching the requested schema
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


def get_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def call_structured(system: str, messages: list[dict], response_schema: dict) -> LLMResult:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    model = get_model()
    body = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": system,
        "messages": messages,
        "tools": [{
            "name": TOOL_NAME,
            "description": "Submit your reply to the owner and the proposed changes.",
            "input_schema": response_schema,
        }],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
    }
    started = time.monotonic()
    try:
        resp = httpx.post(
            API_URL, json=body, timeout=TIMEOUT_SECONDS,
            headers={"x-api-key": api_key, "anthropic-version": API_VERSION,
                     "content-type": "application/json"},
        )
    except httpx.HTTPError as e:
        raise LLMError(f"Could not reach the model provider: {type(e).__name__}")
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code != 200:
        raise LLMError(f"Model provider returned HTTP {resp.status_code}")
    data = resp.json()
    block = next((b for b in data.get("content", []) if b.get("type") == "tool_use"), None)
    if block is None:
        raise LLMError("Model did not return a structured answer")
    usage = data.get("usage", {})
    return LLMResult(
        text=json.dumps(block["input"]),
        model=data.get("model", model),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        latency_ms=latency_ms,
    )


def call_llm(prompt: str, response_schema=None) -> str:
    """Same contract as pipeline.llm_client.call_llm, backed by Anthropic."""
    if response_schema is None:
        response_schema = {"type": "object", "properties": {"text": {"type": "string"}},
                           "required": ["text"]}
    schema = (response_schema.model_json_schema()
              if hasattr(response_schema, "model_json_schema") else response_schema)
    result = call_structured(
        "Answer the user's request.", [{"role": "user", "content": prompt}], schema)
    return result.text
