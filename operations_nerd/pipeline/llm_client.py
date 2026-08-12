"""
Thin wrapper around the Gemini API.

This file knows how to talk to Gemini. It knows nothing about real estate,
prompts, or the pipeline. Everything content-specific (what to ask, what
shape the answer should take) comes from the caller. That boundary matters:
if provider details ever leaked into pipeline logic, or pack content ever
leaked into this file, that would blur the line the whole architecture
depends on.
"""

import os
from google import genai

MODEL = "gemini-3.6-flash"

_client = None


def get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Copy .env.example to .env and fill in your key."
            )
        _client = genai.Client(api_key=api_key)
    return _client


def call_llm(prompt: str, response_schema=None) -> str:
    """Sends prompt to Gemini and returns the raw text response.

    If response_schema is given (a Pydantic model class), asks Gemini for
    structured JSON matching that schema instead of free-form text. The
    caller is responsible for parsing/validating the returned JSON string,
    this function just returns text either way, to keep its own contract
    simple.
    """
    client = get_client()
    kwargs = {"model": MODEL, "input": prompt}
    if response_schema is not None:
        kwargs["response_format"] = {
            "type": "text",
            "mime_type": "application/json",
            "schema": response_schema.model_json_schema(),
        }
    interaction = client.interactions.create(**kwargs)
    return interaction.output_text
