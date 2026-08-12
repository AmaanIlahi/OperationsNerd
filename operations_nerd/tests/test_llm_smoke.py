"""
Smoke test for the Gemini API connection.

Run this yourself after filling in .env with your real GEMINI_API_KEY --
this can't be verified in advance without a real key, so this is the first
thing to run once the key is in place, before trusting anything built on
top of it.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from pydantic import BaseModel
from pipeline.llm_client import call_llm

# 1. Plain text call
print("Plain text call:")
response = call_llm("Reply with exactly the word: pong")
print(" ", response)
assert "pong" in response.lower()

# 2. Structured output call, the pattern the pipeline will actually use
class Extraction(BaseModel):
    intent: str
    urgency: str

print("\nStructured output call:")
response = call_llm(
    "A customer emailed: 'Is the downtown condo still available? I need to know ASAP.' "
    "Extract the intent (a short label) and urgency (low/medium/high).",
    response_schema=Extraction,
)
print(" ", response)
parsed = Extraction.model_validate_json(response)
print(" Parsed:", parsed)

print("\nGemini connection smoke test passed.")
