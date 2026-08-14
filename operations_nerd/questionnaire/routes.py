"""
Questionnaire endpoints.

Two jobs only, matching the Setup Questionnaire box in the architecture:
1. GET  /packs/{pack_id}/questionnaire  -> hand back the questions to render,
   straight from the loaded pack, no industry-specific code here.
2. POST /businesses                      -> validate submitted answers against
   that same pack, then create the business with settings_json filled in.

Every industry-specific detail (which questions exist, what counts as valid)
comes from load_pack(). This module never hardcodes anything about real
estate, or any other vertical.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from packs.loader import load_pack, list_packs, PackLoadError
from questionnaire.validation import validate_answers, answers_to_settings
from db import db as d

router = APIRouter()


class CreateBusinessRequest(BaseModel):
    name: str
    industry_pack: str
    answers: dict


def _load_pack_or_404(pack_id: str):
    try:
        return load_pack(pack_id)
    except PackLoadError as e:
        # A pack that won't load is a server-side config problem, not a client
        # error, but 404 is the honest signal to the frontend: "that pack
        # doesn't exist / isn't usable." Full detail still goes in the body
        # for debugging.
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/packs")
def get_packs():
    return list_packs()


@router.get("/packs/{pack_id}/questionnaire")
def get_questionnaire(pack_id: str):
    pack = _load_pack_or_404(pack_id)
    return {
        "pack_id": pack.id,
        "display_name": pack.display_name,
        "questions": pack.questions,
    }


@router.post("/businesses")
def create_business(payload: CreateBusinessRequest):
    pack = _load_pack_or_404(payload.industry_pack)

    issues = validate_answers(pack, payload.answers)
    if issues:
        raise HTTPException(status_code=422, detail={"issues": issues})

    settings = answers_to_settings(pack, payload.answers)

    with d.get_conn() as conn:
        business_id = d.create_business(
            conn,
            name=payload.name,
            industry_pack=pack.id,
            pack_version=pack.version,
            settings=settings,
        )

    return {
        "business_id": business_id,
        "industry_pack": pack.id,
        "settings": settings,
    }
