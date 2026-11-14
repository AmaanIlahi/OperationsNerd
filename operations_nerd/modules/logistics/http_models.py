"""Shared strict input primitives for authenticated HTTP adapters."""
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt

PositiveInt = Annotated[StrictInt, Field(gt=0)]
PositivePathInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
RequestKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Command(Input):
    idempotency_key: RequestKey
