"""Always-on skills injection for production prompts.

Callers concatenate unconditionally. An empty store (or a store failure) yields
``""`` from :func:`skills_prompt_block`, so the caller's prompt stays
byte-identical to the pre-injection shape.
"""

from __future__ import annotations

from typing import Iterable, Optional

from infona_client.skills.resolve import skills_prompt_block


def entitled_from(obj: object | None) -> bool:
    """``is_entitled(tenant)`` when ``obj`` carries a TenantContext, else False."""
    if obj is None:
        return False
    from infona_client.auth.api_keys import TenantContext
    from infona_client.graph.entitlement import is_entitled

    if isinstance(obj, TenantContext):
        return is_entitled(obj)
    extras = getattr(obj, "extras", None)
    if isinstance(extras, dict):
        tenant = extras.get("tenant")
        if isinstance(tenant, TenantContext):
            return is_entitled(tenant)
    tenant = getattr(obj, "tenant", None)
    if isinstance(tenant, TenantContext):
        return is_entitled(tenant)
    return False


def type_names_for_skills(ontology: str) -> list[str]:
    """Populated types in the planning ontology text; all types if none marked live."""
    from infona_client.nlp.cypher_types import (
        extract_type_activity_from_ontology,
        extract_type_names_from_ontology,
    )

    activity = extract_type_activity_from_ontology(ontology)
    populated = [n for n, count in activity.items() if count != 0]
    return populated or extract_type_names_from_ontology(ontology)


def schema_skills_suffix(schema: Optional[dict]) -> str:
    """Suffix for Type/Attributes/Relationships templates. Empty → ``""``."""
    block = str((schema or {}).get("skills") or "").strip()
    return f"\n{block}" if block else ""


async def ontology_with_skills(
    ontology: str,
    type_names: Iterable[str],
    *,
    tenant_id: str,
    tenant: object | None = None,
    entitled: bool | None = None,
) -> str:
    """Append the skills block to an ontology/context string. Empty block = noop."""
    if entitled is None:
        entitled = entitled_from(tenant)
    block = await skills_prompt_block(
        type_names, tenant_id=tenant_id or "", entitled=bool(entitled)
    )
    if not block:
        return ontology
    if not ontology:
        return block
    return f"{ontology}\n\n{block}"


async def attach_type_skills(
    schema: dict,
    type_name: str,
    tenant_id: str,
    *,
    tenant: object | None = None,
    entitled: bool | None = None,
) -> dict:
    """Copy ``schema`` and set ``skills`` from the injection seam. Never raises."""
    if entitled is None:
        entitled = entitled_from(tenant)
    block = await skills_prompt_block(
        [type_name], tenant_id=tenant_id or "", entitled=bool(entitled)
    )
    out = dict(schema)
    out["skills"] = block
    return out


__all__ = [
    "attach_type_skills",
    "entitled_from",
    "ontology_with_skills",
    "schema_skills_suffix",
    "type_names_for_skills",
]
