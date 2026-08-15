"""Retrieval / ranking / anti-cheat for the example bank.

Looked up on :mod:`infona_client.nlp.example_bank` at call time via ``_host()``
when a sibling needs a patchable name.
"""

from __future__ import annotations

import numpy as np

from infona_client.nlp.example_bank_models import Example, _host


class ExampleBankRetrieveMixin:
    """Owns :meth:`ExampleBank.retrieve`."""

    async def retrieve(
        self,
        question: str,
        ontology_context: str = "",
        exclude_questions: list[str] | None = None,
        kg_name: str = "",
        top_k: int = 3,
        *,
        language: str = "sparql",
    ) -> list[Example]:
        """Retrieve the best few-shot examples for a query.

        Algorithm:
        1. Embed the incoming question.
        2. Cosine similarity against all examples -> top-10 candidates.
        3. EXCLUDE any example whose question similarity > 0.95 to any excluded question (anti-cheat).
        4. EXCLUDE same-dataset examples with similarity > 0.85 (too close).
        5. PREFER cross-dataset examples (different kg_name scores higher).
        6. DIVERSIFY: pick top_k examples with different pattern_tags when possible.

        Args:
            question: The natural language query.
            ontology_context: Current ontology summary (used for re-ranking).
            exclude_questions: Questions to exclude from results (anti-cheat).
            kg_name: The current KG name (for cross-dataset preference).
            top_k: Number of examples to return.
            language: ``"sparql"`` (default) or ``"cypher"`` (Neo4j / ONTA-539).
                Cypher mode only ranks examples that carry a non-empty
                ``cypher`` field so the Neo4j prompt never receives SPARQL-only
                rows that format away to empty. SPARQL mode only ranks rows
                with non-empty ``sparql``.

        Returns:
            List of up to top_k Example objects, pattern-diverse and relevant.
        """
        if not self._examples:
            return []

        host = _host()
        exclude_questions = exclude_questions or []
        lang = (language or "sparql").strip().lower()

        # ONTA-539: pre-filter the pool so cosine never promotes a row that
        # format_examples_for_prompt would drop for this language. Also require
        # a non-empty embedding so the matrix stays well-defined.
        def _in_language_pool(ex: Example) -> bool:
            if not ex.embedding:
                return False
            if lang == "cypher":
                return bool((ex.cypher or "").strip())
            if lang == "sparql":
                return bool((ex.sparql or "").strip())
            # Unknown language: any non-empty query body.
            return bool((ex.sparql or "").strip() or (ex.cypher or "").strip())

        pool_indices = [
            i for i, ex in enumerate(self._examples) if _in_language_pool(ex)
        ]
        if not pool_indices:
            return []

        # Step 1: Embed the question
        q_embedding = await self._embed_single(question)
        q_vec = np.array(q_embedding, dtype=np.float32)

        # Build embedding matrix over the language-filtered pool only.
        pool_examples = [self._examples[i] for i in pool_indices]
        bank_matrix = np.stack(
            [np.array(ex.embedding, dtype=np.float32) for ex in pool_examples]
        )
        similarities = host._cosine_similarity(q_vec, bank_matrix)

        # Step 2: Top-10 candidates by raw similarity (indices into pool_indices)
        pool_rank = np.argsort(similarities)[::-1][:10].tolist()
        candidate_indices = [pool_indices[j] for j in pool_rank]
        # Map global index -> similarity for scoring
        sim_by_global = {
            pool_indices[j]: float(similarities[j]) for j in range(len(pool_indices))
        }

        # Step 3: Anti-cheat — embed excluded questions and filter
        exclude_vecs: list[np.ndarray] = []
        if exclude_questions:
            exclude_embeddings = await self._embed_texts(exclude_questions)
            exclude_vecs = [np.array(e, dtype=np.float32) for e in exclude_embeddings]

        filtered: list[tuple[int, float]] = []  # (index, adjusted_score)
        for idx in candidate_indices:
            ex = self._examples[idx]
            sim = sim_by_global[idx]

            # Anti-cheat: check against excluded questions
            if exclude_vecs:
                ex_vec = np.array(ex.embedding, dtype=np.float32)
                excluded = False
                for ev in exclude_vecs:
                    excl_sim = float(np.dot(ex_vec, ev) / (np.linalg.norm(ex_vec) * np.linalg.norm(ev) + 1e-9))
                    if excl_sim > host.ANTI_CHEAT_THRESHOLD:
                        excluded = True
                        break
                if excluded:
                    continue

            # Step 4: Same-dataset anti-cheat — EVAL ONLY.
            # Gated on exclude_questions so this only runs during eval/benchmark
            # harness calls (which always pass exclude_questions). In production
            # /ask we WANT to reuse a near-identical prior answer on the same KG:
            # that's the best possible signal, not cheating. Dropping/penalizing
            # it here would actively hurt real users. Keep this in sync with the
            # anti-cheat gate above (line ~321).
            if exclude_vecs and kg_name and ex.kg_name == kg_name:
                if sim > host.SAME_DATASET_MAX_SIM:
                    continue  # Too similar within same dataset
                # Penalize same-dataset slightly to prefer cross-dataset
                sim *= 0.9

            filtered.append((idx, sim))

        if not filtered:
            return []

        # Sort by adjusted score
        filtered.sort(key=lambda x: x[1], reverse=True)

        # Step 5 & 6: Diversify by pattern_tags
        selected: list[Example] = []
        used_tag_sets: list[set[str]] = []

        for idx, _score in filtered:
            if len(selected) >= top_k:
                break
            ex = self._examples[idx]
            ex_tags = set(ex.pattern_tags)

            # Check if this example's tags are too similar to already-selected ones
            if selected and ex_tags:
                too_similar = False
                for used in used_tag_sets:
                    if used and ex_tags == used:
                        too_similar = True
                        break
                if too_similar:
                    # Still consider it if we haven't filled slots
                    continue

            selected.append(ex)
            used_tag_sets.append(ex_tags)

        # If diversity filtering was too aggressive, backfill from remaining
        if len(selected) < top_k:
            selected_set = {id(ex) for ex in selected}
            for idx, _score in filtered:
                if len(selected) >= top_k:
                    break
                ex = self._examples[idx]
                if id(ex) not in selected_set:
                    selected.append(ex)
                    selected_set.add(id(ex))

        return selected[:top_k]
