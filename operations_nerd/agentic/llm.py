"""
Provider-neutral LLM layer for the agentic CRM.

The agent turn needs exactly one thing from a model: structured JSON for a
system prompt plus chat messages. Every provider implements that one method
(LLMClient.call_structured) and returns an LLMResult, so the agent turn,
validator and audit trail do not care which provider answered.

    LLM_PROVIDER = gemini | anthropic | stub      (default: gemini)

Provider modules are imported only when selected, so a missing SDK or key for
one provider never affects another. Tests replace agentic.chat.run_llm.
"""

import os
from dataclasses import dataclass
from typing import Protocol

PROVIDERS = ("gemini", "anthropic", "stub")
DEFAULT_PROVIDER = "gemini"


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str                 # JSON text matching the requested schema
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class LLMClient(Protocol):
    def call_structured(self, system: str, messages: list[dict], response_schema: dict) -> LLMResult: ...


def get_provider() -> str:
    name = (os.environ.get("LLM_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    if name not in PROVIDERS:
        raise LLMError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)} (got {name!r})")
    return name


def required_key_env(provider: str | None = None) -> str | None:
    """Name of the API-key env var the selected provider needs, if any."""
    return {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider or get_provider())


class _StubClient:
    """No network, no credits: answers every turn with no proposed changes."""

    def call_structured(self, system, messages, response_schema):
        import json
        return LLMResult(
            text=json.dumps({"reply": "(stub assistant) I am not connected to a model, so I have no changes to propose.",
                             "operations": []}),
            model="stub", input_tokens=0, output_tokens=0, latency_ms=0)


class _GeminiClient:
    def call_structured(self, system, messages, response_schema):
        from agentic import gemini_llm
        return gemini_llm.call_structured(system, messages, response_schema)


class _AnthropicClient:
    def call_structured(self, system, messages, response_schema):
        from agentic import anthropic_llm
        return anthropic_llm.call_structured(system, messages, response_schema)


_CLIENTS = {"gemini": _GeminiClient(), "anthropic": _AnthropicClient(), "stub": _StubClient()}


def get_client() -> LLMClient:
    return _CLIENTS[get_provider()]


def call_structured(system: str, messages: list[dict], response_schema: dict) -> LLMResult:
    """The default agent-turn LLM call; the provider is read per call."""
    return get_client().call_structured(system, messages, response_schema)
