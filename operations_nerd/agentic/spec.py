"""
Spec model for the agentic CRM (see documentation/agentic_crm_architecture.md).

A spec is the whole definition of one business's CRM: its entity types,
their fields, and the links between entity types. It is stored as JSON, one
row per version. Keys are permanent identifiers generated here on the
server; the LLM never supplies them. Labels are free text the owner can change.
"""

import re
import secrets
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

FIELD_TYPES = ("text", "long_text", "number", "date", "boolean",
               "select", "multiselect", "email", "phone")
CARDINALITIES = ("one_to_one", "many_to_one", "one_to_many", "many_to_many")

ENTITY_PREFIX = "crm_"
FIELD_PREFIX = "f_"
LINK_PREFIX = "l_"

ENTITY_KEY_RE = re.compile(r"^crm_[a-z0-9_]{1,40}$")
FIELD_KEY_RE = re.compile(r"^f_[a-z0-9_]{1,40}$")
LINK_KEY_RE = re.compile(r"^l_[a-z0-9_]{1,40}$")

MAX_LABEL_LENGTH = 100


class _Strict(BaseModel):
    # Unknown attributes are an error: a spec never silently carries
    # something the engine does not understand.
    model_config = ConfigDict(extra="forbid")


class SpecField(_Strict):
    key: str
    label: str
    type: Literal["text", "long_text", "number", "date", "boolean",
                  "select", "multiselect", "email", "phone"]
    options: Optional[list[str]] = None
    required: bool = False
    archived: bool = False


class SpecEntity(_Strict):
    key: str
    label: str
    archived: bool = False
    fields: list[SpecField] = Field(default_factory=list)


class SpecLink(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str
    label: str
    # "from" is a Python keyword, so the attribute is from_ with a JSON alias.
    from_: str = Field(alias="from")
    to: str
    cardinality: Literal["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
    archived: bool = False
    # Set only when the link was archived as a side effect of archiving this
    # entity type, so restoring the entity brings back exactly those links.
    archived_by: Optional[str] = None


class SpecBusiness(_Strict):
    name: str
    industry: Optional[str] = None


class Spec(_Strict):
    business: SpecBusiness
    entities: list[SpecEntity] = Field(default_factory=list)
    links: list[SpecLink] = Field(default_factory=list)


def empty_spec(name: str, industry: Optional[str] = None) -> dict:
    return {"business": {"name": name, "industry": industry}, "entities": [], "links": []}


# ---------- server-generated keys ----------

def _new_key(prefix: str, taken: set[str]) -> str:
    while True:
        key = prefix + secrets.token_hex(4)
        if key not in taken:
            taken.add(key)
            return key


def new_entity_key(taken: set[str]) -> str:
    return _new_key(ENTITY_PREFIX, taken)


def new_field_key(taken: set[str]) -> str:
    return _new_key(FIELD_PREFIX, taken)


def new_link_key(taken: set[str]) -> str:
    return _new_key(LINK_PREFIX, taken)


def rekey_spec(spec: dict) -> dict:
    """Returns a copy of `spec` with every entity, field and link key replaced
    by a freshly generated one, with link endpoints remapped to match. Used
    when a template is turned into a business, so keys are always server
    generated and unique per business, whatever the template file contains."""
    taken: set[str] = set()
    entity_map = {e["key"]: new_entity_key(taken) for e in spec["entities"]}
    out = {"business": dict(spec["business"]), "entities": [], "links": []}
    for e in spec["entities"]:
        out["entities"].append({
            **e,
            "key": entity_map[e["key"]],
            "fields": [{**f, "key": new_field_key(taken)} for f in e["fields"]],
        })
    for l in spec["links"]:
        out["links"].append({
            **l,
            "key": new_link_key(taken),
            "from": entity_map[l["from"]],
            "to": entity_map[l["to"]],
        })
    return out
