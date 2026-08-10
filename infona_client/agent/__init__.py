"""Unified Ask-AI agent: one conversational endpoint + a capability registry.

See :mod:`infona_client.agent.registry` for the capability protocol and
:mod:`infona_client.agent.planner` for the classify→plan→confirm→execute flow.
"""

from infona_client.agent.registry import (
    AgentCapability,
    AgentContext,
    PlanStep,
    get_capabilities,
    get_capability,
    register_capability,
)

__all__ = [
    "AgentCapability",
    "AgentContext",
    "PlanStep",
    "get_capabilities",
    "get_capability",
    "register_capability",
]
