"""Baseten Model API provider; only validated JSON enters the proposal engine.

Official API: https://docs.baseten.co/reference/inference-api/chat-completions
Structured output: https://docs.baseten.co/inference/structured-outputs
Requested model: https://www.baseten.co/library/gpt-oss-120b
Credentials and optional endpoint/model settings are server environment only.
"""
import copy
import json
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from agentic.llm import LLMError, LLMResult

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"
MAX_OUTPUT_TOKENS = 8192
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 65536
TIMEOUT = httpx.Timeout(connect=10, read=90, write=30, pool=10)


def get_model():
    model = (os.environ.get("BASETEN_MODEL") or DEFAULT_MODEL).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise LLMError("BASETEN_MODEL must be a valid model slug")
    return model


def get_endpoint():
    base = (os.environ.get("BASETEN_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        parsed = urlsplit(base)
        trusted = parsed.hostname == "inference.baseten.co" or bool(re.fullmatch(
            r"model-[a-z0-9-]+\.api\.baseten\.co", parsed.hostname or ""))
        path_ok = parsed.path == "/v1" or bool(re.fullmatch(r"/environments/[a-zA-Z0-9_-]{1,64}/sync/v1", parsed.path))
        if (parsed.scheme != "https" or not trusted or not path_ok or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise LLMError("BASETEN_BASE_URL must be a trusted HTTPS Baseten /v1 endpoint without credentials or query parameters") from None
    return base + "/chat/completions"


def typed_schema(response_schema):
    """Give each spec operation its actual shape, rather than optional soup.

    The exact operations still pass through the existing server validator and
    approval loop. This function neither applies nor grants new operations.
    """
    schema = copy.deepcopy(response_schema)
    items = schema.get("properties", {}).get("operations", {}).get("items", {})
    if "op" in items.get("properties", {}):
        from agentic.operations import Operation
        schema["properties"]["operations"]["items"] = {"anyOf": [
            model.model_json_schema(by_alias=True) for model in Operation.__args__[0].__args__]}
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    return schema


def _tokens(value):
    return value if type(value) is int and value >= 0 else None


def _answer(data, requested_model, latency_ms):
    try:
        choice = data["choices"][0]
        message = choice["message"]
        if choice.get("finish_reason") != "stop":
            raise LLMError("Baseten did not complete the structured answer; shorten the request and try again")
        if message.get("refusal"):
            raise LLMError("Baseten declined to provide a structured answer")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise LLMError("Baseten did not return a bounded structured answer")
        if not isinstance(json.loads(text), dict):
            raise LLMError("Baseten did not return a JSON object")
        usage = data.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        model = data.get("model")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
            model = requested_model
        return LLMResult(text=text, model=model, input_tokens=_tokens(usage.get("prompt_tokens")),
                         output_tokens=_tokens(usage.get("completion_tokens")), latency_ms=latency_ms)
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise LLMError("Baseten returned an unreadable structured answer") from None


def call_structured(system, messages, response_schema):
    key = (os.environ.get("BASETEN_API_KEY") or "").strip()
    if not key:
        raise LLMError("BASETEN_API_KEY is not set")
    model, endpoint = get_model(), get_endpoint()
    body = {"model": model, "messages": [{"role": "system", "content": system}, *messages],
            "stream": False, "max_tokens": MAX_OUTPUT_TOKENS, "temperature": 0.2, "reasoning_effort": "low",
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "operations_nerd_response", "strict": True, "schema": typed_schema(response_schema)}}}
    started = time.monotonic()
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
            with client.stream("POST", endpoint, json=body,
                               headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}) as response:
                if response.status_code != 200:
                    # Provider errors can echo private inputs or credentials.
                    # Never read, log or return the response body or URL here.
                    raise LLMError(f"Baseten returned HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise LLMError("Baseten response exceeded the size limit")
                    chunks.append(chunk)
        data = json.loads(b"".join(chunks))
    except httpx.HTTPError as error:
        raise LLMError(f"Could not reach Baseten ({type(error).__name__})") from None
    except (ValueError, UnicodeError):
        raise LLMError("Baseten returned an unreadable response") from None
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    return _answer(data, model, latency_ms)
