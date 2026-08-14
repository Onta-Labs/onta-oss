from __future__ import annotations

"""Background governance hooks for brand-new types.

Job: optionally propose a newly minted type to the judge panel. Never
blocks ingest; never writes instance facts.
"""

# Call-time host lookups so tests that patch schema_resolver.logger /
# insert_facts / _entity_uri / env flags keep working after this extract.
import asyncio

from infona_client.graph.iri import IRI_BASE
from infona_client.resolver.models import ExtractedEntity
from infona_client.resolver import schema_resolver as _sr


class SchemaGovernanceMixin:
    """Governance half of SchemaResolver — opt-in, non-blocking."""

    async def _maybe_govern_new_type(self, entity: ExtractedEntity, graph_uri: str) -> None:
        """Governance seam (ADR 0002 §2, COG-43): propose a brand-new type for
        the shared Global-Public layer and, on majority judge approval, write
        a governed copy there with provenance + changelog.

        The tenant-layer write has ALREADY happened (todayf's behavior — the
        tenant uses the type immediately whatever the verdict); approval only
        ADDS a Public-layer copy.

        Scheduling (COG-46): the judge panel + Public-layer write run as a
        BACKGROUND task — ingest never waits on LLM judges. Semantics are
        eventually consistent: an approved type appears in the Public layer
        shortly AFTER ingest returns. Task references are retained on
        ``self._governance_tasks``; await :meth:`drain_governance` to
        deterministically wait for all scheduled outcomes. Best-effort: any
        failure (scheduling or in-task) is logged and never blocks or crashes
        ingest. No-op when INFONA_GOVERNANCE_ENABLED is off (default).
        """
        if not self._governance_enabled:
            return
        from infona_client.resolver.governance import TypeProposal
        try:
            graphs_prefix = f"{IRI_BASE}/graphs/"
            tenant_id = (
                graph_uri[len(graphs_prefix):] if graph_uri.startswith(graphs_prefix) else graph_uri
            )
            proposal = TypeProposal(
                type_name=entity.type_name,
                parent_chain=list(entity.parent_chain),
                tenant_id=tenant_id,
                reasoning=(
                    f"Extractor proposed brand-new type '{entity.type_name}' "
                    f"matching no existing ontology type"
                ),
                proposer_model=self.EXTRACT_MODEL,
            )
            # Drop references to finished tasks so the list stays bounded on
            # long-lived resolvers, then schedule the panel off the ingest path.
            self._governance_tasks = [t for t in self._governance_tasks if not t.done()]
            self._governance_tasks.append(
                asyncio.create_task(self._govern_in_background(proposal))
            )
        except Exception:
            _sr.logger.warning("governance_failed", type_name=entity.type_name, exc_info=True)

    async def _govern_in_background(self, proposal) -> None:
        """Run propose-and-judge + the Public-layer write off the ingest path
        (COG-46). Exceptions are logged and swallowed here, inside the task —
        a governance failure never crashes ingest and never surfaces as an
        unretrieved task exception.
        """
        try:
            decision = await self._governance.propose_and_judge(proposal, self._judge_panel)
            if decision.approved:
                await self._governance.write_governed_type(proposal, decision)
            else:
                _sr.logger.info("governance_type_tenant_only", type_name=proposal.type_name)
        except Exception:
            _sr.logger.warning("governance_failed", type_name=proposal.type_name, exc_info=True)

    async def drain_governance(self) -> None:
        """Await all pending background governance tasks (COG-46).

        Governance is eventually consistent: :meth:`_maybe_govern_new_type`
        schedules the judge panel + Public-layer write as background tasks,
        so an approved type appears in the Public layer shortly after ingest
        returns. Call this to deterministically wait for every scheduled
        outcome — tests, and callers that need the Public layer settled
        before reading it. Safe to call any time (no-op with nothing
        pending). Task failures were already logged inside the tasks and are
        never re-raised here.
        """
        tasks, self._governance_tasks = self._governance_tasks, []
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
