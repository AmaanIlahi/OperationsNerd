"""
Event intake endpoint.

For this POC, events are submitted directly (paste an email in, or send via
API), rather than through a real inbox integration. What matters is that
everything after intake, extraction, action_type selection, drafting, is
identical to what a real integration would trigger.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from db import db as d
from packs.loader import load_pack, PackLoadError
from pipeline.service import process_event, PipelineError

router = APIRouter()


class CreateEventRequest(BaseModel):
    business_id: int
    source: str
    raw_content: str
    contact_id: int | None = None


@router.post("/events")
def create_event(payload: CreateEventRequest):
    with d.get_conn() as conn:
        business = d.get_business(conn, payload.business_id)
        if not business:
            raise HTTPException(status_code=404, detail=f"No business with id {payload.business_id}")

        try:
            pack = load_pack(business["industry_pack"])
        except PackLoadError as e:
            raise HTTPException(status_code=500, detail=str(e))

        event_id = d.create_event(
            conn,
            business_id=payload.business_id,
            source=payload.source,
            raw_content=payload.raw_content,
            contact_id=payload.contact_id,
        )

        try:
            result = process_event(conn, pack, event_id)
        except PipelineError as e:
            raise HTTPException(status_code=422, detail=str(e))

    return {"event_id": event_id, "drafted_action": result}
