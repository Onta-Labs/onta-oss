from enum import Enum
from pydantic import BaseModel, Field


class FunctionTier(str, Enum):
    PLATFORM = "platform"
    CUSTOM = "custom"


class FunctionRef(BaseModel):
    name: str
    entity_type: str
    description: str = ""
    endpoint_url: str | None = None
    tier: FunctionTier = FunctionTier.CUSTOM


class FunctionRegister(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(min_length=1, max_length=200)
    endpoint_url: str = Field(description="HTTPS endpoint for the function")
    description: str = ""


class FunctionResult(BaseModel):
    output: dict
    duration_ms: float
    function_name: str
