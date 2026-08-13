"""
Event Pipeline.

Turns one raw inbound event into a drafted action, using only what the pack
declares. Two LLM calls:

1. Extraction: pull structured fields out of the raw event, AND have the
   model choose an action_type -- constrained to exactly the action types
   this pack's event schema allows, and further narrowed to only the ones
   this pack has actually implemented a draft prompt for. This is "Option B"
   routing: the LLM picks from the pack's own declared vocabulary, so there
   is never an `if event_type == "inbound_email"` anywhere in this file.

2. Drafting: render the draft prompt whose key matches the chosen
   action_type exactly, and ask the model to write the actual content.

Why action_type is constrained rather than free-text: an earlier smoke test
showed Gemini returning free-form labels for unconstrained fields (e.g.
"Check Property Availability" instead of a fixed value). If action_type
were extracted the same way, it could come back close-but-not-exact and
silently fail to match a prompt key. Constraining it via an enum in the
response schema removes that failure mode entirely, rather than defending
against it after the fact.
"""

from db import db as d
from packs.loader import Pack
from pipeline.llm_client import call_llm
from pipeline.templating import render_template
import json


class PipelineError(Exception):
    pass


def _find_event_schema(pack: Pack, event_type: str):
    for schema in pack.event_schemas:
        if schema.event_type == event_type:
            return schema
    return None


def _build_extraction_schema(extract_fields: list[str], usable_action_types: list[str]) -> dict:
    """JSON schema for the extraction call's structured output. Every
    extract_field the pack declares becomes a plain string field; action_type
    is the one field with real constraints, an enum of only the action types
    this pack can currently act on."""
    properties = {field: {"type": "string"} for field in extract_fields}
    properties["action_type"] = {"type": "string", "enum": usable_action_types}
    return {
        "type": "object",
        "properties": properties,
        "required": extract_fields + ["action_type"],
    }


def process_event(conn, pack: Pack, event_id: int) -> dict:
    """Runs the full pipeline for one event: extract -> choose action ->
    draft. Returns the created drafted_action row. Raises PipelineError for
    conditions that mean this pack can't currently handle this event --
    those are candidates for the falsifiability log, not silent failures."""

    event = d.get_event(conn, event_id)
    if not event:
        raise PipelineError(f"No event with id {event_id}")

    business = d.get_business(conn, event["business_id"])

    schema = _find_event_schema(pack, event["source"])
    if not schema:
        raise PipelineError(
            f"Pack '{pack.id}' has no event schema for source '{event['source']}'"
        )

    usable_action_types = [at for at in schema.valid_action_types if at in pack.prompts]
    if not usable_action_types:
        raise PipelineError(
            f"Pack '{pack.id}' declares valid_action_types for '{schema.event_type}' "
            f"but none of them have a matching prompt yet: {schema.valid_action_types}"
        )

    extraction_template_key = f"extract_{schema.event_type}"
    if extraction_template_key not in pack.prompts:
        raise PipelineError(
            f"Pack '{pack.id}' has no prompt named '{extraction_template_key}' "
            f"for event type '{schema.event_type}'"
        )

    # ---- 1. Extraction call ----
    context = {"settings": business["settings_json"], "event": {"raw_content": event["raw_content"]}}
    extraction_prompt = render_template(pack.prompts[extraction_template_key], context)
    extraction_prompt += (
        f"\n\nAlso choose the single best action_type from this list: "
        f"{usable_action_types}. Return it as \"action_type\"."
    )

    extraction_schema = _build_extraction_schema(schema.extract_fields, usable_action_types)
    raw_extraction = call_llm(extraction_prompt, response_schema=extraction_schema)

    parsed = json.loads(raw_extraction)
    action_type = parsed["action_type"]

    d.mark_event_processed(conn, event_id, parsed_data=parsed)

    # ---- 2. Drafting call ----
    draft_context = {
        "settings": business["settings_json"],
        "event": {"raw_content": event["raw_content"], "parsed_data": parsed},
    }
    draft_prompt = render_template(pack.prompts[action_type], draft_context)
    draft_text = call_llm(draft_prompt)

    drafted_action_id = d.create_drafted_action(
        conn,
        event_id=event_id,
        business_id=event["business_id"],
        contact_id=event.get("contact_id"),
        action_type=action_type,
        payload={"body": draft_text},
    )

    return {
        "id": drafted_action_id,
        "action_type": action_type,
        "payload": {"body": draft_text},
        "parsed_data": parsed,
    }
