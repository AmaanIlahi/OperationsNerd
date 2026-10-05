"""
LLM client with pluggable providers.

This file knows how to talk to a model. It knows nothing about real estate,
prompts, or the pipeline. Everything content-specific (what to ask, what
shape the answer should take) comes from the caller. That boundary matters:
if provider details ever leaked into pipeline logic, or pack content ever
leaked into this file, that would blur the line the whole architecture
depends on.

Provider is chosen with LLM_PROVIDER (default "gemini", so existing setups
keep working):

  gemini   Google Gemini via google-genai, key in GEMINI_API_KEY
  openai   OpenAI chat completions, key in OPENAI_API_KEY (OPENAI_MODEL)
  ollama   local model, OLLAMA_MODEL, OLLAMA_URL (default http://localhost:11434)
  mock     deterministic, offline. For tests, demos and first runs.

Transient failures (rate limits, timeouts, 5xx) are retried with backoff.
LLM_MAX_RETRIES (default 2) sets how many extra tries.
"""

import json
import os
import re
import time
import urllib.request
import urllib.error

MODEL = "gemini-3.6-flash"

_client = None


class LLMError(RuntimeError):
    pass


def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Copy .env.example to .env and fill in your key."
            )
        from google import genai
        _client = genai.Client(api_key=api_key)
    return _client


def _schema_dict(response_schema):
    if response_schema is None:
        return None
    return response_schema.model_json_schema() if hasattr(response_schema, "model_json_schema") else response_schema


def _gemini(prompt, response_schema):
    client = get_client()
    kwargs = {"model": MODEL, "input": prompt}
    schema_dict = _schema_dict(response_schema)
    if schema_dict is not None:
        kwargs["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema_dict}
    return client.interactions.create(**kwargs).output_text


def _post_json(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code} from model server") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"model server unreachable: {getattr(e, 'reason', e)}") from None


def _json_instruction(schema_dict):
    return (
        "\n\nReturn only a JSON object that matches this JSON schema, with no extra text:\n"
        + json.dumps(schema_dict)
    )


def _openai(prompt, response_schema):
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY not set.")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    schema_dict = _schema_dict(response_schema)
    body = {"model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt + (_json_instruction(schema_dict) if schema_dict else "")}]}
    if schema_dict:
        body["response_format"] = {"type": "json_object"}
    r = _post_json(base + "/chat/completions", body, {"Authorization": "Bearer " + key})
    return r["choices"][0]["message"]["content"]


def _ollama(prompt, response_schema):
    base = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    schema_dict = _schema_dict(response_schema)
    body = {"model": os.environ.get("OLLAMA_MODEL", "llama3.1"), "stream": False,
            "messages": [{"role": "user", "content": prompt}]}
    if schema_dict:
        body["format"] = schema_dict
    return _post_json(base + "/api/chat", body, {})["message"]["content"]


def _mock(prompt, response_schema):
    """Deterministic offline stand-in. With a schema it fills every field: enums take their
    first value that appears in the prompt text (else the first), strings echo a short snippet."""
    schema_dict = _schema_dict(response_schema)
    if schema_dict is None:
        first = re.sub(r"\s+", " ", prompt.strip())[:160]
        return "Thanks for reaching out. [mock draft] " + first
    out = {}
    for name, spec in (schema_dict.get("properties") or {}).items():
        if "enum" in spec:
            low = prompt.lower()
            hit = next((e for e in spec["enum"] if str(e).lower().replace("_", " ") in low), None)
            out[name] = hit or spec["enum"][0]
        else:
            out[name] = "mock " + name
    return json.dumps(out)


PROVIDERS = {"gemini": _gemini, "openai": _openai, "ollama": _ollama, "mock": _mock}


def _transient(exc):
    text = str(exc)
    return isinstance(exc, LLMError) and any(c in text for c in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "unreachable")) \
        or any(c in text for c in ("429", "503", "timed out", "Timeout", "RESOURCE_EXHAUSTED"))


def call_llm(prompt: str, response_schema=None) -> str:
    """Sends prompt to the configured model and returns the raw text response.

    response_schema can be a Pydantic model class (its .model_json_schema()
    is used), or a raw JSON schema dict directly -- the pipeline needs the
    dict form, since valid action_types come from the pack at runtime and
    can't be expressed as a static Pydantic model known in advance.

    The caller is responsible for parsing/validating the returned JSON
    string; this function just returns text either way, to keep its own
    contract simple.
    """
    name = os.environ.get("LLM_PROVIDER", "gemini").strip().lower()
    if name not in PROVIDERS:
        raise RuntimeError(f"Unknown LLM_PROVIDER '{name}'. Use one of: {', '.join(sorted(PROVIDERS))}")
    fn = PROVIDERS[name]
    retries = int(os.environ.get("LLM_MAX_RETRIES", "2"))
    for attempt in range(retries + 1):
        try:
            return fn(prompt, response_schema)
        except Exception as exc:
            if attempt == retries or not _transient(exc):
                raise
            time.sleep(0.5 * (2 ** attempt))
