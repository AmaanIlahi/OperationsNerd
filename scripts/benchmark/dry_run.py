"""
Scripted stand-in for the model, used by `run_benchmark.py --dry-run`.

It reads the CURRENT_SPEC the real agent would read (from the system prompt)
and the owner's message, and answers like a competent agent would, so the
harness, the checks and the report can be exercised end to end without a
network or any credits. It recognises the benchmark's own message templates;
it is not a model and says nothing about model quality.
"""

import json
import re

from agentic.llm import LLMResult

DRY_RUN_MODEL = "dry-run-stub"
TOKENS_IN, TOKENS_OUT, LATENCY_MS = 400, 120, 5

CREATE_OPS = {
    "groomer": [
        {"op": "create_entity_type", "label": "Owner", "ref": "owner"},
        {"op": "add_field", "entity": "owner", "label": "Full name", "type": "text", "required": True},
        {"op": "add_field", "entity": "owner", "label": "Phone", "type": "phone"},
        {"op": "create_entity_type", "label": "Dog", "ref": "dog"},
        {"op": "add_field", "entity": "dog", "label": "Name", "type": "text", "required": True},
        {"op": "add_field", "entity": "dog", "label": "Breed", "type": "text"},
        {"op": "add_field", "entity": "dog", "label": "Size", "type": "select", "options": ["Small", "Medium", "Large"]},
        {"op": "add_field", "entity": "dog", "label": "Notes", "type": "long_text"},
        {"op": "create_entity_type", "label": "Appointment", "ref": "appt"},
        {"op": "add_field", "entity": "appt", "label": "Date", "type": "date"},
        {"op": "add_field", "entity": "appt", "label": "Service", "type": "text"},
        {"op": "add_link", "label": "Owner", "from": "dog", "to": "owner", "cardinality": "many_to_one"},
        {"op": "add_link", "label": "Dog", "from": "appt", "to": "dog", "cardinality": "many_to_one"},
    ],
    "bakery": [
        {"op": "create_entity_type", "label": "Customer", "ref": "cust"},
        {"op": "add_field", "entity": "cust", "label": "Full name", "type": "text", "required": True},
        {"op": "add_field", "entity": "cust", "label": "Email", "type": "email"},
        {"op": "create_entity_type", "label": "Product", "ref": "prod"},
        {"op": "add_field", "entity": "prod", "label": "Name", "type": "text", "required": True},
        {"op": "add_field", "entity": "prod", "label": "Price", "type": "number"},
        {"op": "create_entity_type", "label": "Order", "ref": "ord"},
        {"op": "add_field", "entity": "ord", "label": "Pickup date", "type": "date"},
        {"op": "add_field", "entity": "ord", "label": "Paid", "type": "boolean"},
        {"op": "add_link", "label": "Customer", "from": "ord", "to": "cust", "cardinality": "many_to_one"},
    ],
}


def _spec(system: str) -> dict:
    return json.loads(system.split("<CURRENT_SPEC>")[1].split("</CURRENT_SPEC>")[0])


def _entity(spec, label):
    return next((e for e in spec["entities"] if e["label"].lower() == label.lower()), None)


def _field(entity, label):
    return next((f for f in entity["fields"] if f["label"].lower() == label.lower()), None)


def plan(spec: dict, message: str) -> tuple[str, list]:
    m = message
    if "I run a gym" in m:
        return "You already have a gym CRM from the template; nothing to change.", []
    if "dog grooming" in m:
        return "Here is a grooming CRM.", CREATE_OPS["groomer"]
    if "neighbourhood bakery" in m:
        return "Here is a bakery CRM.", CREATE_OPS["bakery"]
    if (r := re.search(r"Add a field called 'Referral source' to (.+?)\.", m)):
        e = _entity(spec, r.group(1))
        return "Added.", [{"op": "add_field", "entity": e["key"], "label": "Referral source", "type": "select",
                           "options": ["Walk-in", "Referral", "Online", "Other"]}]
    if (r := re.search(r"On (.+?), the '(.+?)' field needs to hold longer answers\. Change its type to (.+?)\.", m)):
        e = _entity(spec, r.group(1))
        new_type = {"long text": "long_text", "text": "text"}.get(r.group(3).lower(), "text")
        return "Changed.", [{"op": "change_field_type", "entity": e["key"], "field": _field(e, r.group(2))["key"], "type": new_type}]
    if (r := re.search(r"rename the '(.+?)' field on (.+?) to '(.+?)'", m)):
        e = _entity(spec, r.group(2))
        return "Renamed.", [{"op": "update_field", "entity": e["key"], "field": _field(e, r.group(1))["key"], "label": r.group(3)}]
    if "'Feedback'" in m:
        e = _entity(spec, re.search(r"link each Feedback to a (.+?)\.", m).group(1))
        return "Added Feedback.", [
            {"op": "create_entity_type", "label": "Feedback", "ref": "fb"},
            {"op": "add_field", "entity": "fb", "label": "Rating", "type": "number"},
            {"op": "add_field", "entity": "fb", "label": "Comment", "type": "long_text"},
            {"op": "add_link", "label": "About", "from": "fb", "to": e["key"], "cardinality": "many_to_one"}]
    if (r := re.search(r"Each (.+?) should be linked to a (.+?)\. Please add that link", m)):
        a, b = _entity(spec, r.group(1)), _entity(spec, r.group(2))
        return "Linked.", [{"op": "add_link", "label": b["label"], "from": a["key"], "to": b["key"], "cardinality": "many_to_one"}]
    if (r := re.search(r"remove the '(.+?)' field from (.+?),|never use the '(.+?)' field on (.+?)\. Please remove", m)):
        field, ent = (r.group(1), r.group(2)) if r.group(1) else (r.group(3), r.group(4))
        e = _entity(spec, ent)
        return "Removed (archived).", [{"op": "archive_field", "entity": e["key"], "field": _field(e, field)["key"]}]
    if (r := re.search(r"I need the '(.+?)' field on (.+?) back", m)):
        e = _entity(spec, r.group(2))
        return "Restored.", [{"op": "restore_field", "entity": e["key"], "field": _field(e, r.group(1))["key"]}]
    if (r := re.search(r"Create another entity type called '(.+?)'", m)):
        return "Created.", [{"op": "create_entity_type", "label": r.group(1)}]
    if "'hologram'" in m:
        e = _entity(spec, re.search(r"to (.+?) with the type", m).group(1))
        return "Added.", [{"op": "add_field", "entity": e["key"], "label": "Mood", "type": "hologram"}]
    if (r := re.search(r"Delete the (.+?) entity completely", m)):
        e = _entity(spec, r.group(1))
        return "I cannot delete data; I can archive it, which hides it but keeps the records.", \
               [{"op": "archive_entity_type", "entity": e["key"]}]
    return "I did not understand.", []


class DryRunLLM:
    """Same call shape as agentic.llm.call_structured. Counts its calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, system, messages, schema):
        self.calls += 1
        reply, ops = plan(_spec(system), messages[0]["content"] if len(messages) == 1 else
                          next(m["content"] for m in messages if m["role"] == "user"))
        return LLMResult(json.dumps({"reply": reply, "operations": ops}), DRY_RUN_MODEL,
                         TOKENS_IN, TOKENS_OUT, LATENCY_MS)
