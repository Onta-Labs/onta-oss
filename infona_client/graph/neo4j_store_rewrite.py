"""Entity-id rewrite + rebind helpers for :class:`Neo4jGraphSession`.

Cypher strings are copied verbatim — do not change query semantics.
"""

from __future__ import annotations

from infona_client.graph.store import require_entity_write_identity


class Neo4jRewriteMixin:
    """write_rewrite_entity_id + Assertion / ProvEvent rebind."""

    async def write_rewrite_entity_id(self, old_id: str, new_id: str) -> None:
        """Re-key Entity ``id``; rebind incident rels + Assertion + ProvEvent.

        * **Free ``new_id``:** ``SET old.id = new_id`` (relationships stay on the
          same node). ``Assertion.subject_id`` / ``object_id`` and
          ``ProvEvent.subject_id`` are rewritten to match.
        * **``new_id`` already exists (ER merge):** rebind every incident
          relationship onto the survivor, re-point Assertion SUBJECT/OBJECT
          edges + denormalized ids, re-point ``:ABOUT`` / ``subject_id`` on
          ``:ProvEvent``, coalesce display props onto survivor, then
          ``DETACH DELETE`` the loser — never drop edges with the node.
        """
        require_entity_write_identity({"id": old_id})
        require_entity_write_identity({"id": new_id})
        if old_id == new_id:
            return

        old_rows = await self.execute_read(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "RETURN e.id AS id",
            {"old_id": old_id},
        )
        if not old_rows:
            return

        neu_rows = await self.execute_read(
            "MATCH (e:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "RETURN e.id AS id",
            {"new_id": new_id},
        )
        if not neu_rows:
            # Free id: re-key in place; relationships stay bound to the node.
            await self.execute_write(
                "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
                "SET old.id = $new_id\n"
                "RETURN old.id AS id",
                {"old_id": old_id, "new_id": new_id},
            )
            await self._rebind_prov_subject_ids(old_id, new_id)
            await self._rebind_assertion_ids(old_id, new_id)
            return

        # Target exists — rebind endpoints onto survivor, then drop loser.
        # List outbound + inbound (self-loops only once via outbound arm).
        rel_rows = await self.execute_read(
            "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})"
            "-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg})\n"
            "RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type, "
            "coalesce(r.attr, '') AS attr\n"
            "UNION\n"
            "MATCH (a:Entity {tenant_id: $tenant_id, kg: $kg})"
            "-[r]->(b:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "WHERE a.id <> $old_id\n"
            "RETURN a.id AS start_id, b.id AS end_id, type(r) AS rel_type, "
            "coalesce(r.attr, '') AS attr",
            {"old_id": old_id},
        )
        for row in rel_rows:
            start = str(row.get("start_id") or "")
            end = str(row.get("end_id") or "")
            rel_type = str(row.get("rel_type") or "")
            attr = str(row.get("attr") or "")
            if not start or not end or not rel_type:
                continue
            new_start = new_id if start == old_id else start
            new_end = new_id if end == old_id else end
            # Prefer original attr leaf when present so sanitize_rel_type is stable.
            attr_leaf = attr if attr else rel_type.lower()
            await self.write_merge_rel(new_start, new_end, rel_type, attr_leaf)

        # Re-point Assertions BEFORE DETACH DELETE (SUBJECT/OBJECT + denorm ids).
        await self._rebind_assertions_merge_into(old_id, new_id)

        # Re-point :ABOUT edges and subject_id before DETACH DELETE removes them.
        await self.execute_write(
            "MATCH (p:ProvEvent {tenant_id: $tenant_id, kg: $kg})"
            "-[a:ABOUT]->(old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "SET p.subject_id = $new_id\n"
            "CREATE (p)-[:ABOUT]->(neu)\n"
            "DELETE a\n"
            "RETURN p.subject_id AS subject_id",
            {"old_id": old_id, "new_id": new_id},
        )
        await self._rebind_prov_subject_ids(old_id, new_id)
        # Rebind AttrCitation ownership onto survivor.
        await self.execute_write(
            "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})"
            "-[hc:HAS_CITATION]->(c:AttrCitation {tenant_id: $tenant_id, kg: $kg})\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "SET c.entity_id = $new_id\n"
            "MERGE (neu)-[:HAS_CITATION]->(c)\n"
            "DELETE hc\n"
            "RETURN c.entity_id AS entity_id",
            {"old_id": old_id, "new_id": new_id},
        )
        await self.execute_write(
            "MATCH (c:AttrCitation {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE c.entity_id = $old_id\n"
            "SET c.entity_id = $new_id\n"
            "RETURN count(c) AS n",
            {"old_id": old_id, "new_id": new_id},
        )

        # Survivor wins on conflict; fill gaps from loser (display props).
        await self.execute_write(
            "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "SET neu.primary_type = coalesce(neu.primary_type, old.primary_type),\n"
            "    neu.name = coalesce(neu.name, old.name),\n"
            "    neu.source = coalesce(neu.source, old.source)\n"
            "RETURN neu.id AS id",
            {"old_id": old_id, "new_id": new_id},
        )
        # DETACH DELETE only after rebind — edges already live on survivor.
        await self.execute_write(
            "MATCH (old:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "DETACH DELETE old\n"
            "RETURN $new_id AS id",
            {"old_id": old_id, "new_id": new_id},
        )

    async def _rebind_prov_subject_ids(self, old_id: str, new_id: str) -> None:
        """Rewrite ``ProvEvent.subject_id`` (and leave ABOUT for free-id path)."""
        await self.execute_write(
            "MATCH (p:ProvEvent {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE p.subject_id = $old_id\n"
            "SET p.subject_id = $new_id\n"
            "RETURN count(p) AS n",
            {"old_id": old_id, "new_id": new_id},
        )
        # Free-id path: AttrCitation.entity_id must track the re-key too.
        await self.execute_write(
            "MATCH (c:AttrCitation {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE c.entity_id = $old_id\n"
            "SET c.entity_id = $new_id\n"
            "RETURN count(c) AS n",
            {"old_id": old_id, "new_id": new_id},
        )

    async def _rebind_assertion_ids(self, old_id: str, new_id: str) -> None:
        """Free-id path: rewrite denormalized Assertion subject_id / object_id.

        SUBJECT/OBJECT edges stay attached to the re-keyed Entity node; only
        the denormalized properties need updating.
        """
        await self.execute_write(
            "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE a.subject_id = $old_id\n"
            "SET a.subject_id = $new_id\n"
            "RETURN count(a) AS n",
            {"old_id": old_id, "new_id": new_id},
        )
        await self.execute_write(
            "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE a.object_id = $old_id\n"
            "SET a.object_id = $new_id\n"
            "RETURN count(a) AS n",
            {"old_id": old_id, "new_id": new_id},
        )

    async def _rebind_assertions_merge_into(self, old_id: str, new_id: str) -> None:
        """ER merge: move Assertion SUBJECT/OBJECT links + denorm ids to survivor.

        Runs before DETACH DELETE of the loser so Assertions are not orphaned.
        """
        # SUBJECT side: denorm + edge
        await self.execute_write(
            "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE a.subject_id = $old_id\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "OPTIONAL MATCH (a)-[old_s:SUBJECT]->"
            "(:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "DELETE old_s\n"
            "SET a.subject_id = $new_id\n"
            "MERGE (a)-[:SUBJECT]->(neu)\n"
            "RETURN count(a) AS n",
            {"old_id": old_id, "new_id": new_id},
        )
        # OBJECT side (object properties + reverse refs)
        await self.execute_write(
            "MATCH (a:Assertion {tenant_id: $tenant_id, kg: $kg})\n"
            "WHERE a.object_id = $old_id\n"
            "MATCH (neu:Entity {tenant_id: $tenant_id, kg: $kg, id: $new_id})\n"
            "OPTIONAL MATCH (a)-[old_o:OBJECT]->"
            "(:Entity {tenant_id: $tenant_id, kg: $kg, id: $old_id})\n"
            "DELETE old_o\n"
            "SET a.object_id = $new_id\n"
            "MERGE (a)-[:OBJECT]->(neu)\n"
            "RETURN count(a) AS n",
            {"old_id": old_id, "new_id": new_id},
        )
