"""
Human Approval endpoints.

Exposes the pending-approval queue and the approve/reject actions a
business owner takes on a drafted action.
"""

from fastapi import APIRouter, HTTPException

from db import db as d
from approval.service import approve_and_send, reject_action, ApprovalError

router = APIRouter()


@router.get("/businesses/{business_id}/pending-actions")
def get_pending_actions(business_id: int):
    with d.get_conn() as conn:
        return d.list_pending_actions(conn, business_id)


@router.post("/drafted-actions/{action_id}/approve")
def approve(action_id: int):
    with d.get_conn() as conn:
        try:
            return approve_and_send(conn, action_id)
        except ApprovalError as e:
            raise HTTPException(status_code=409, detail=str(e))


@router.post("/drafted-actions/{action_id}/reject")
def reject(action_id: int):
    with d.get_conn() as conn:
        try:
            return reject_action(conn, action_id)
        except ApprovalError as e:
            raise HTTPException(status_code=409, detail=str(e))
