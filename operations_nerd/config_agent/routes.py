"""
/config-agent endpoints for the browser demo (frontend_agent/index.html).

Only mounted when CONFIG_AGENT_ENABLED=1 (see main.py) -- these endpoints
write files into the repo's pack folders, so they must be off by default
and never enabled on a deployed server.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config_agent.service import propose, apply, ConfigAgentError

router = APIRouter(prefix="/config-agent")


class ProposeRequest(BaseModel):
    pack_id: str
    request: str


class ApplyRequest(BaseModel):
    pack_id: str
    file: str
    proposed_content: str
    base_hash: str


@router.post("/propose")
def propose_change(payload: ProposeRequest):
    try:
        result = propose(payload.pack_id, payload.request)
    except ConfigAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "file": result["target_file"],
        "diff": result["diff"],
        "validation": result["validation"],
        "errors": result["errors"],
        "proposed_content": result["new_content"],
        "base_hash": result["base_hash"],
        "explanation": result["explanation"],
    }


@router.post("/apply")
def apply_change(payload: ApplyRequest):
    try:
        apply(payload.pack_id, payload.file, payload.proposed_content, payload.base_hash)
    except ConfigAgentError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"applied": True, "file": payload.file}
