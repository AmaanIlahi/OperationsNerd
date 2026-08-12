"""
State endpoint.

This is NOT one of the three architecture boxes (Setup Questionnaire, Event
Pipeline, Human Approval). It's observability tooling: a single read-only
view of everything the system knows about one business, used by the frontend
to show live state and highlight what changed. It exists to make the
config-only thesis visible, not to do any work itself.
"""

from fastapi import APIRouter, HTTPException

from db import db as d

router = APIRouter()


@router.get("/businesses/{business_id}/state")
def get_business_state(business_id: int):
    with d.get_conn() as conn:
        business = d.get_business(conn, business_id)
        if not business:
            raise HTTPException(status_code=404, detail=f"No business with id {business_id}")

        return {
            "business": business,
            "contacts": d.list_contacts(conn, business_id),
            "follow_ups": d.list_follow_ups(conn, business_id),
            "events": d.list_events(conn, business_id),
            "drafted_actions": d.list_drafted_actions(conn, business_id),
            "approval_policies": d.list_approval_policies(conn, business_id),
        }
