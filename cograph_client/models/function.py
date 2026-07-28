from enum import Enum
from typing import Literal, Optional

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
    #: Ontology layer the function is attached to (ONTA-399). Defaults to
    #: tenant for back-compat; Enhanced attachments report ``"enhanced"``.
    layer: str = "tenant"


class FunctionRegister(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    entity_type: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Type to attach to. Bare name → Tenant; full Enhanced URI or "
            "path-shaped 'x/<Type>' → Enhanced; Public is refused (ONTA-400)."
        ),
    )
    endpoint_url: str = Field(description="HTTPS endpoint for the function")
    description: str = ""
    #: Optional explicit layer. When omitted, inferred from ``entity_type``.
    #: ``"public"`` is always refused. ``"enhanced"`` is operator-only on the
    #: HTTP route (workspace ordinary writes stay on Tenant).
    layer: Optional[Literal["tenant", "enhanced", "public"]] = None


class FunctionResult(BaseModel):
    output: dict
    duration_ms: float
    function_name: str
