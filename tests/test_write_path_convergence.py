"""Drift guard: every write of KG instance data MUST funnel through the one
shared write path (graph/kg_writer.py) — inserts via ``insert_facts``, removals
via ``delete_facts``, URI rewrites via ``rewrite_subject``, and post-write
housekeeping via ``refresh_after_write``.

The bug this prevents: a writer hand-rolls its own insert/delete and skips the
shared housekeeping, so its writes serve stale embeddings / a stale NL-planning
cache, un-batched inserts, or (ADR 0007) leave ghost rows in a derived secondary
index keyed to a subject that was deleted or merged away. Ingestion/enrichment
already drifted this way once; ``api/routes/lambda_functions.py`` drifted a
second time and the *enumerated* guard couldn't see it because ``api/routes/``
wasn't on its list.

Shape (ADR 0007 §4): the structural guard is now **scan-everything-with-allowlist**
instead of enumerate-known-writers. It scans ALL of ``infona_client/`` for
bespoke instance-write markers and **fails by default** on any hit outside an
explicit, justified allowlist — so a NEW writer that hand-rolls a write is caught
even if nobody remembered to add it to a list.

Two layers, as before:
- **Behavioral** — drive a real write entrypoint and assert it invoked the shared
  housekeeping (the part that drifts).
- **Structural** — the deny-by-default source scan + positive assertions that the
  converged sites route through the primitives.
"""

import inspect
import io
import pathlib
import re
import tokenize
from unittest.mock import AsyncMock, patch

import infona_client
import infona_client.api.routes.ingest as ingest_route_mod
import infona_client.enrichment.executor as executor_mod
import infona_client.resolver.schema_resolver as schema_resolver_mod


def _calls(src: str, name: str) -> bool:
    """True if ``src`` contains a CALL to ``name`` (``name(``) at a word boundary,
    so ``insert_triples`` does not match inside ``batched_insert_triples``."""
    return re.search(rf"(?<![\w.]){re.escape(name)}\(", src) is not None


# --- Behavioral: the JSON/free-text ingest method delegates housekeeping -------


@patch("infona_client.api.routes.ingest.refresh_after_write", new_callable=AsyncMock)
@patch("infona_client.api.routes.ingest.SchemaResolver")
def test_ingest_route_delegates_housekeeping_to_shared_writer(
    mock_resolver_cls, mock_refresh, client, auth_headers
):
    """POST /ingest must run its post-write refresh through the shared
    refresh_after_write — not a re-inlined embed/cache-invalidate."""
    from infona_client.resolver.models import IngestResult

    inst = AsyncMock()
    inst.ingest.return_value = IngestResult(
        entities_extracted=1,
        entities_resolved=1,
        triples_inserted=3,
        types_created=["Property"],
        attributes_added=["Property.price"],
    )
    mock_resolver_cls.return_value = inst

    resp = client.post(
        "/graphs/test-tenant/ingest",
        json={"content": "a house at 1 Main St for $500,000", "source": "t", "kg_name": "k"},
        headers=auth_headers,
    )

    assert resp.status_code == 200
    assert mock_refresh.await_count == 1
    kwargs = mock_refresh.await_args.kwargs
    assert kwargs["tenant_id"] == "test-tenant"
    assert kwargs["kg_name"] == "k"
    # types_created + the type of every attributes_added entry.
    assert kwargs["affected_types"] == {"Property"}


# --- Structural tripwire: deny-by-default scan of the whole package ------------

# Markers of a bespoke instance-graph write (ADR 0007 §4 + ONTA-528):
#   M1 — a call to the single-statement insert builder ``insert_triples(`` (the
#        un-batched / un-refreshed insert smell). Word-boundary so the bare name
#        is not matched inside ``batched_insert_triples`` (that combo is M4).
#   M2 — hand-written SPARQL removal against a graph: ``DELETE {``, ``DELETE WHERE``,
#        or ``DELETE DATA``. SQL ``DELETE FROM ...`` (the Postgres durable stores:
#        job/plan/schedule/stats/spatiotemporal) is intentionally NOT matched — it
#        is not a triple write.
#   M4 — ``batched_insert_triples(...)`` composed with a SPARQL client
#        ``.update(...)`` flush (ONTA-528). On a Neo4j-only product that path is
#        the silent write-drop: the SPARQL goes to the vestigial NeptuneClient,
#        the facts never land, and a premature ``triples_inserted += len(...)``
#        still reports success. The builder alone (in ``graph/queries.py``) is
#        fine; the *flush* is the violation. Instance writes must use
#        ``insert_facts``.
_M1 = re.compile(r"(?<![\w.])insert_triples\(")
_M2 = re.compile(r"DELETE\s*\{|DELETE\s+WHERE|DELETE\s+DATA")
_M4_BATCHED = re.compile(r"(?<![\w.])batched_insert_triples\(")
_M4_SPARQL_UPDATE = re.compile(
    r"(?:await\s+)?(?:self\._neptune|neptune|client|n)\.update\s*\("
)
# E3 / Neo4j: free-form instance CREATE/MERGE outside the write path (or schema
# bootstrap) is the Cypher-era twin of bespoke insert_triples. Allowlisted
# modules own the only sanctioned emitters.
_M3 = re.compile(
    r"(?<![\w.])(?:execute_write)\s*\(\s*(?:f?['\"]|cypher\s*=)",
    re.IGNORECASE,
)
_M3b = re.compile(
    r"""(?:CREATE|MERGE)\s+\([a-zA-Z_][a-zA-Z0-9_]*\s*:(?:Entity|ProvEvent)\b""",
    re.IGNORECASE,
)

# Deny-by-default allowlist: the ONLY modules permitted to construct raw SPARQL
# instance/graph writes, each with the reason it is not a convergence violation.
# A new writer that hand-rolls an instance write lands OUTSIDE this list and fails
# by default (that is the whole point — enumerating writers let lambda_functions
# drift because api/routes/ was simply never added).
#
# Deliberately NOT here (ADR 0007 named them, but they compose the sanctioned
# batched builders / build triple lists and construct NO raw markers, so they
# never trip the scan — a stronger property than being allowlisted, and
# ``test_allowlist_entries_are_live`` keeps them out): ``graph/kg_writer.py`` (the
# converged write path — uses batched_insert/delete_triples + rewrite_subject_update)
# and ``graph/provenance.py`` (returns triple lists, no SPARQL string construction).
_ALLOWLIST: dict[str, str] = {
    # Residual SPARQL value-history companion inside the converged writer
    # (pre-Neo4j history port). Instance inserts go through _insert_facts_store /
    # pg_ops only; this entry exists solely because M4 flags the history path's
    # batched_insert_triples + neptune.update. Remove when history is ported.
    "graph/kg_writer.py": "converged write path — residual value-history SPARQL companion still uses batched_insert_triples + neptune.update (ONTA-528 allowlist until history GraphStore port); instance inserts are store-path only.",
    # The SPARQL builder library the whole write path composes.
    "graph/queries.py": "SPARQL builder library — defines the insert/delete/rewrite statement builders every writer composes; not itself a writer.",
    # Non-instance graphs: schema / aliases / governance.
    # ONTA-403: ontology_queries.py is reclassified as a SPARQL *builder*
    # library (mirrors graph/queries.py) — it constructs schema upsert DELETE/
    # INSERT strings but never calls neptune.update. Application of those
    # builders is exclusive to graph/ontology_commit.py, enforced by
    # tests/test_ontology_commit_convergence.py.
    "graph/ontology_queries.py": "ontology SCHEMA SPARQL builder library (ONTA-403) — defines type/attribute/range/comment builders; not itself a writer. Application is exclusive to ontology_commit.py.",
    "graph/ontology_commit.py": "ontology SCHEMA commit path (ONTA-403) — the one place that applies ontology_queries builders; emits changelog + revision, not instance data.",
    "graph/ontology_snapshots.py": "ontology SCHEMA snapshot/diff/restore (ONTA-406) — versions named graphs + RDF release records; not instance data. Copy/clear/drop of schema graphs + insert_triples for release metadata only.",
    "graph/ontology_base_pin.py": "ontology SCHEMA-META workspace base pin (ONTA-405) — CLEAR + insert_triples on per-tenant base-pin companion graph only; not instance data.",
    # ONTA-407b: aliases stay allowlisted with an honest sole-caller justification.
    # register_alias / retire_alias are applied only by ontology_commit
    # (REGISTER_ALIAS / RENAME_ATTRIBUTE / RETIRE_ALIAS); backfill_aliases is the
    # sole instance-predicate rewrite entrypoint for the alias lifecycle. Not a
    # general instance writer — domain facts still go through kg_writer.
    "graph/aliases.py": "aliasOf SPARQL writer + lazy instance-predicate backfill (ADR 0002 §7) — sole production callers are ontology_commit (register/rename/retire) and the alias backfill entrypoint (ONTA-407b). Not a general instance writer.",
    "resolver/governance.py": "audit / changelog / governance-provenance graphs — not instance data (ADR 0007 allowlist).",
    # Derived / admin escape hatches with their own lifecycle.
    "resolver/functions.py": "derived computed-function value store (ADR 0002 §6 / ADR 0001 rule 6) — regenerable /derived/ values with their own atomic replace + TTL/invalidate lifecycle, never asserted facts.",
    "api/routes/knowledge_graphs.py": "KG-lifecycle admin (create/delete KG, triple-count metadata) — graph lifecycle, not instance-fact writing (ADR 0007 allowlist: whole-graph admin ops).",
}

# Cypher-era allowlist (E3): modules that may emit Entity/ProvEvent CREATE|MERGE
# or free-form execute_write with instance Cypher. Everyone else must go through
# kg_writer → pg_ops → session write_* / execute_template.
_CYPHER_WRITE_ALLOWLIST: dict[str, str] = {
    "graph/kg_writer.py": "converged write path — dual-backend dispatcher; does not hand-roll instance Cypher (uses pg_ops).",
    "graph/pg_ops.py": "property-graph write ops — delegates to session write_* / execute_template only.",
    "graph/schema_bootstrap.py": "allowlisted Cypher template registry + schema constraints (not a free-form instance writer).",
    "graph/labels.py": "domain-label SET builder (sanitized tokens) used by set_entity_type_labels.",
    "graph/memory_store.py": "hermetic GraphStore — native write_* + smoke templates for tests.",
    "graph/neo4j_store.py": "Neo4j GraphStore — write_* methods emit scoped Cypher with sanitized prop/rel tokens.",
    "graph/store.py": "GraphStore protocol + scope gates; no instance CREATE/MERGE payloads.",
    "graph/facts.py": "Fact IR + sanitizers; no store I/O.",
}

_PKG_ROOT = pathlib.Path(infona_client.__file__).parent


def _strip_comments(src: str) -> str:
    """Blank out ``#`` COMMENT token spans, preserving line/column structure.

    Keeps string literals (so real SPARQL inside f-strings is still scanned) but
    removes prose comments like ``# one DELETE/WHERE per type`` or ``# DELETE it``
    that would otherwise be false positives. Docstrings are string tokens and are
    kept — the allowlisted modules are the only ones whose docstrings mention
    DELETE, so this needs no special-casing."""
    lines = src.splitlines(keepends=True)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return src
    for tok in toks:
        if tok.type != tokenize.COMMENT:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            line = lines[srow - 1]
            lines[srow - 1] = line[:scol] + " " * (ecol - scol) + line[ecol:]
    return "".join(lines)


def _bespoke_markers(code: str) -> list[str]:
    marks = []
    if _M1.search(code):
        marks.append("insert_triples(")
    if _M2.search(code):
        marks.append("raw SPARQL DELETE")
    # ONTA-528: the Neo4j-era silent-drop pattern.
    if _M4_BATCHED.search(code) and _M4_SPARQL_UPDATE.search(code):
        marks.append("batched_insert_triples+neptune.update")
    return marks


def _cypher_instance_write_markers(code: str) -> list[str]:
    """Markers for free-form Neo4j instance CREATE/MERGE (E3 drift guard)."""
    marks = []
    if _M3b.search(code):
        marks.append("CREATE/MERGE Entity|ProvEvent Cypher")
    return marks


def test_no_bespoke_instance_write_outside_allowlist():
    """Scan ALL of ``infona_client/`` for bespoke instance-write markers and fail
    on any hit outside the justified allowlist (ADR 0007 §4).

    This replaces the old enumerate-known-writers guard, which structurally
    couldn't see a writer that was never added to its list (the exact way
    api/routes/lambda_functions.py drifted). Deny-by-default means the next such
    writer fails here without anyone having to remember to extend a list."""
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        code = _strip_comments(path.read_text())
        marks = _bespoke_markers(code)
        if marks and rel not in _ALLOWLIST:
            violations.append(f"{rel}: {', '.join(marks)}")
    assert not violations, (
        "Bespoke instance-graph write markers found OUTSIDE the write-path "
        "convergence allowlist. Route these through graph/kg_writer.py "
        "(insert_facts / delete_facts / rewrite_subject + refresh_after_write), "
        "or — if the module legitimately writes a non-instance graph "
        "(ontology/governance/admin) — add it to _ALLOWLIST with a one-line "
        "justification. Offenders:\n  " + "\n  ".join(violations)
    )


def test_no_freeform_cypher_instance_write_outside_allowlist():
    """E3: scan for CREATE/MERGE of Entity|ProvEvent outside the GraphStore /
    kg_writer surface. Application code must not invent a second Cypher write
    path (ADR 0012 + model §8)."""
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if rel in _CYPHER_WRITE_ALLOWLIST:
            continue
        code = _strip_comments(path.read_text())
        marks = _cypher_instance_write_markers(code)
        if marks:
            violations.append(f"{rel}: {', '.join(marks)}")
    assert not violations, (
        "Free-form Entity/ProvEvent CREATE|MERGE Cypher found outside the E3 "
        "write-path allowlist. Route instance mutations through "
        "graph/kg_writer.py (store/session path → pg_ops). Offenders:\n  "
        + "\n  ".join(violations)
    )


def test_allowlist_entries_are_live():
    """Every allowlist entry must still trip a marker — a stale entry (a module
    that no longer hand-rolls a write, or was deleted) is dead weight that hides
    the guard's real surface. Keep the allowlist honest."""
    stale = []
    for rel in _ALLOWLIST:
        path = _PKG_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
            continue
        if not _bespoke_markers(_strip_comments(path.read_text())):
            stale.append(f"{rel} (no bespoke markers — remove from _ALLOWLIST)")
    assert not stale, "Stale write-path allowlist entries:\n  " + "\n  ".join(stale)


# --- Structural: the converged sites route through the primitives --------------


def test_lambda_functions_route_uses_primitives():
    """api/routes/lambda_functions.py (the site that drifted) materializes
    lambda-computed attributes through delete_facts + insert_facts + one
    refresh_after_write — no raw DELETE, no bare insert_triples."""
    import infona_client.api.routes.lambda_functions as mod

    src = inspect.getsource(mod)
    assert _calls(src, "delete_facts"), "lambda route must clear old values via kg_writer.delete_facts"
    assert _calls(src, "insert_facts"), "lambda route must write new values via kg_writer.insert_facts"
    assert _calls(src, "refresh_after_write"), (
        "lambda route must run post-write housekeeping via kg_writer.refresh_after_write"
    )
    assert not _calls(src, "insert_triples"), (
        "lambda route reintroduced a bespoke insert_triples — route it through kg_writer.insert_facts"
    )
    assert not _M2.search(_strip_comments(src)), (
        "lambda route reintroduced a hand-rolled SPARQL DELETE — route it through kg_writer.delete_facts"
    )


def test_er_rebuild_uses_rewrite_subject():
    """resolver/er/rebuild.py folds every ER merge through kg_writer.rewrite_subject
    (a re-key event, so derived indexes re-key instead of leaving ghosts) and
    refreshes once per batch — no hand-rolled DELETE/INSERT merge SPARQL."""
    import infona_client.resolver.er.rebuild as mod

    src = inspect.getsource(mod)
    assert _calls(src, "rewrite_subject"), "ER rebuild must merge via kg_writer.rewrite_subject"
    assert _calls(src, "refresh_after_write"), (
        "ER rebuild must re-key derived indexes via kg_writer.refresh_after_write(rewritten_subjects=...)"
    )
    assert not _M2.search(_strip_comments(src)), (
        "ER rebuild reintroduced a hand-rolled DELETE/INSERT merge — use kg_writer.rewrite_subject"
    )


def test_normalization_rules_store_uses_primitives():
    """normalization/rules.py persists a rule (metadata subject) via
    delete_facts (clear) + insert_facts (write) — no bespoke insert_triples or
    hand-rolled delete-by-subject SPARQL."""
    import infona_client.normalization.rules as mod

    src = inspect.getsource(mod)
    assert _calls(src, "delete_facts"), "rule store must clear prior triples via kg_writer.delete_facts"
    assert _calls(src, "insert_facts"), "rule store must write via kg_writer.insert_facts"
    assert not _calls(src, "insert_triples"), (
        "rule store reintroduced a bespoke insert_triples — route it through kg_writer.insert_facts"
    )
    assert not _M2.search(_strip_comments(src)), (
        "rule store reintroduced a hand-rolled DELETE — route it through kg_writer.delete_facts"
    )


# --- Structural: pre-existing writers still route through the shared path -------


def test_enrichment_writer_uses_shared_path_not_bespoke_insert():
    """The enrichment executor writes via insert_facts + refresh_after_write and
    must NOT reintroduce a bare ``insert_triples`` instance write."""
    src = inspect.getsource(executor_mod)
    assert _calls(src, "insert_facts"), "enrichment must write via kg_writer.insert_facts"
    assert _calls(src, "refresh_after_write"), "enrichment must refresh via kg_writer.refresh_after_write"
    assert not _calls(src, "insert_triples"), (
        "enrichment reintroduced a bespoke insert_triples write — route it "
        "through graph/kg_writer.insert_facts (write-path convergence rule)."
    )


def test_ingest_writer_uses_shared_insert_and_refresh():
    """The ingest resolver writes through insert_facts, and the ingest routes
    delegate housekeeping to refresh_after_write rather than re-inlining the
    embed / cache-invalidate steps.

    ONTA-528: schema_resolver must not flush via NeptuneClient.update (dead
    SPARQL client on Neo4j-only) or compose batched_insert_triples + update.
    """
    resolver_src = inspect.getsource(schema_resolver_mod)
    assert _calls(resolver_src, "insert_facts"), (
        "ingest resolver must write instance facts via kg_writer.insert_facts"
    )
    clean = _strip_comments(resolver_src)
    assert not _M4_SPARQL_UPDATE.search(clean), (
        "schema_resolver reintroduced NeptuneClient.update — instance writes and "
        "rollbacks must not call the dead SPARQL client (ONTA-528)."
    )
    assert not _M4_BATCHED.search(clean), (
        "schema_resolver reintroduced batched_insert_triples — flush via "
        "kg_writer.insert_facts (ONTA-528)."
    )
    assert "batched_insert_triples+neptune.update" not in _bespoke_markers(clean)

    route_src = inspect.getsource(ingest_route_mod)
    assert _calls(route_src, "refresh_after_write"), (
        "ingest routes must run post-write housekeeping via kg_writer.refresh_after_write"
    )
    # Housekeeping must be DELEGATED — re-inlining these is exactly the drift.
    assert not _calls(route_src, "embed_types"), (
        "ingest route re-inlined embed_types — delegate to refresh_after_write"
    )
    assert "invalidate_cache" not in route_src, (
        "ingest route re-inlined the ontology-cache invalidation — delegate to "
        "refresh_after_write"
    )


def test_normalization_writer_uses_shared_path():
    """normalization/execute.py mutates instance data; its inserts go through
    insert_facts, its removals through delete_facts, and its post-write
    housekeeping through refresh_after_write — no bespoke batched insert,
    stats-recompute, or raw delete_triples."""
    import infona_client.normalization.execute as norm_mod

    src = inspect.getsource(norm_mod)
    assert _calls(src, "insert_facts"), "normalization must insert via kg_writer.insert_facts"
    assert _calls(src, "delete_facts"), "normalization must remove via kg_writer.delete_facts"
    assert _calls(src, "refresh_after_write"), (
        "normalization must run housekeeping via kg_writer.refresh_after_write"
    )
    assert not _calls(src, "batched_insert_triples"), (
        "normalization reintroduced a bespoke batched_insert_triples — route inserts "
        "through kg_writer.insert_facts (write-path convergence rule)."
    )
    assert not _calls(src, "delete_triples"), (
        "normalization reintroduced a bespoke delete_triples — route removals "
        "through kg_writer.delete_facts (write-path convergence rule)."
    )
    assert "_schedule_stats_recompute" not in src, (
        "normalization reintroduced a bespoke stats-recompute — use "
        "kg_writer.refresh_after_write."
    )


def test_dedupe_writers_use_shared_refresh():
    """The dedupe / entity-resolution writers (which mutate counts, not schema)
    must run their post-write refresh through the shared refresh_after_write, not
    a bare schedule_recompute."""
    import infona_client.agent.capabilities.dedup_cap as dedup_mod
    import infona_client.api.routes.actions as actions_mod

    for mod, name in [(dedup_mod, "dedup_cap"), (actions_mod, "actions")]:
        src = inspect.getsource(mod)
        assert _calls(src, "refresh_after_write"), (
            f"{name} dedupe must refresh via kg_writer.refresh_after_write"
        )
        assert not _calls(src, "schedule_recompute"), (
            f"{name} reintroduced a bare schedule_recompute after a graph mutation — "
            "use kg_writer.refresh_after_write."
        )


def test_web_ingest_calls_refresh_after_write():
    """Web-discovery ingest (agent/capabilities/web_ingest_cap.py) CREATES new
    types/attributes/entities via the ingest engine, so its background job must run
    the same post-write housekeeping as every other writer — otherwise the
    ontology expansion stays invisible to NL planning + Explorer. The refresh must
    go through the shared refresh_after_write, not a re-inlined embed/cache step."""
    import infona_client.agent.capabilities.web_ingest_cap as web_ingest_mod

    src = inspect.getsource(web_ingest_mod)
    assert _calls(src, "refresh_after_write"), (
        "web-discovery ingest must run post-write housekeeping via "
        "kg_writer.refresh_after_write"
    )
    assert not _calls(src, "embed_types"), (
        "web ingest re-inlined embed_types — delegate to refresh_after_write"
    )
    assert "invalidate_cache" not in src, (
        "web ingest re-inlined the ontology-cache invalidation — delegate to "
        "refresh_after_write"
    )


def test_shared_writer_is_the_single_housekeeping_owner():
    """Sanity: the shared writer itself is the one place embed/cache-invalidate/
    recompute AND the removal primitives live, so delegating to it actually
    centralizes the behavior."""
    import infona_client.graph.kg_writer as kg_writer_mod

    src = inspect.getsource(kg_writer_mod)
    assert _calls(src, "batched_insert_triples"), "insert_facts must batch"
    assert "embed_types" in src
    assert "invalidate_cache" in src
    assert "schedule_recompute" in src
    # The removal / rewrite primitives + their refresh hooks live here too.
    assert "async def delete_facts" in src
    assert "async def rewrite_subject" in src
    assert "deleted_subjects" in src
    assert "rewritten_subjects" in src


# --- Structural tripwire: semantic-index writers (ONTA-181) --------------------
#
# Background workers escaped the original tripwire (it only covered request-path
# writers). The semantic instance index has exactly TWO writers — the kg_writer
# hook (freshness) and the reconciler (correctness) — and both must stay on the
# shared seams: extraction via extract_semantic_chunks (one chunking/hashing
# contract), writes via the SemanticIndex protocol (upsert_chunks / delete /
# fill_embeddings / mark_embed_failed), and embeddings via the ONE shared embed
# client (nlp/embed_client.embed_texts). Kept as a separate, clearly-named test
# so a parallel extension of this file (route scanning) never collides with it.


def test_semantic_index_writers_use_shared_seams():
    """ONTA-181 drift guard for the semantic-index write hook + reconciler."""
    import infona_client.graph.kg_writer as kg_writer_mod
    import infona_client.semantic.reconciler as reconciler_mod

    # The write hook (kg_writer._index_semantic) chunks via the shared extractor
    # and writes via the protocol — no bespoke chunker/hasher, no direct rows.
    kg_src = inspect.getsource(kg_writer_mod)
    assert _calls(kg_src, "extract_semantic_chunks"), (
        "the kg_writer semantic hook must chunk via semantic.extract."
        "extract_semantic_chunks (one chunking/hashing contract)"
    )
    assert ".upsert_chunks(" in kg_src, (
        "the kg_writer semantic hook must write via SemanticIndex.upsert_chunks"
    )

    # The reconciler: same extractor + protocol writes + the ONE embed client.
    rec_src = inspect.getsource(reconciler_mod)
    assert _calls(rec_src, "extract_semantic_chunks"), (
        "the reconciler must re-extract via extract_semantic_chunks — a private "
        "chunker would fork the content_hash contract"
    )
    assert ".upsert_chunks(" in rec_src, (
        "the reconciler must upsert via SemanticIndex.upsert_chunks"
    )
    assert _calls(rec_src, "embed_texts"), (
        "the embed-fill sweep must embed via nlp.embed_client.embed_texts — the "
        "single embed-batch implementation (ONTA-174)"
    )
    assert ".fill_embeddings(" in rec_src and ".mark_embed_failed(" in rec_src, (
        "the sweep must persist fills/failures via the protocol's "
        "fill_embeddings / mark_embed_failed (content-hash guarded)"
    )
    assert "httpx" not in rec_src, (
        "the reconciler re-inlined an HTTP embedding call — use "
        "nlp.embed_client.embed_texts (shared embed client rule)."
    )
    # The reconciler is NOT an instance-data writer: it must never write
    # Neptune instance triples (its only Neptune writes are textKind ontology
    # markers via upsert_attribute_text_kind).
    assert not _calls(rec_src, "insert_facts")
    assert not _calls(rec_src, "insert_triples")
    assert not _calls(rec_src, "batched_insert_triples"), (
        "the reconciler grew a bespoke Neptune instance write — instance data "
        "goes through graph/kg_writer.insert_facts (write-path convergence rule)."
    )


# --- Guard self-tests: the scan actually catches planted bespoke writes ---------


def test_guard_flags_planted_insert_triples():
    """A hand-rolled insert_triples( call is detected (M1)."""
    planted = "async def f(g, t):\n    return await neptune.update(insert_triples(g, t))\n"
    assert "insert_triples(" in _bespoke_markers(_strip_comments(planted))


def test_guard_flags_planted_sparql_delete():
    """Hand-written SPARQL DELETE string construction is detected (M2)."""
    for planted in (
        'q = f"DELETE {{ GRAPH <{g}> {{ <{s}> ?p ?o }} }} WHERE {{ ... }}"',
        'q = f"WITH <{g}> DELETE WHERE {{ <{s}> ?p ?o }}"',
        'q = f"DELETE DATA {{ GRAPH <{g}> {{ {body} }} }}"',
    ):
        assert "raw SPARQL DELETE" in _bespoke_markers(_strip_comments(planted)), planted


def test_guard_ignores_sql_delete_from():
    """Postgres ``DELETE FROM`` (the durable stores) is NOT a triple write and must
    not be flagged."""
    planted = 'sql = f"DELETE FROM {table} WHERE tenant_id = $1 AND entity_uri = $2"'
    assert _bespoke_markers(_strip_comments(planted)) == []


def test_guard_ignores_delete_facts_and_rewrite_subject():
    """The convergence primitives themselves are not markers (no bespoke write)."""
    planted = (
        "await delete_facts(n, g, subjects=x)\n"
        "await rewrite_subject(n, g, old, new)\n"
        "await insert_facts(n, g, triples)\n"
    )
    assert _bespoke_markers(_strip_comments(planted)) == []


def test_guard_flags_batched_insert_plus_neptune_update():
    """ONTA-528: batched_insert_triples + client.update is the silent-drop pattern.

    The old M1 lookbehind deliberately ignored batched_insert_triples — correct
    when that builder *was* the sanctioned write, wrong once GraphStore is the
    only backend and the SPARQL flush is dead.
    """
    planted = (
        "for s in batched_insert_triples(g, t):\n"
        "    await neptune.update(s)\n"
    )
    marks = _bespoke_markers(_strip_comments(planted))
    assert "batched_insert_triples+neptune.update" in marks, marks

    planted_self = (
        "for sparql in batched_insert_triples(instance_graph, triples):\n"
        "    await self._neptune.update(sparql)\n"
    )
    marks_self = _bespoke_markers(_strip_comments(planted_self))
    assert "batched_insert_triples+neptune.update" in marks_self, marks_self

    # Builder alone (queries.py style) is fine — no flush.
    builder_only = "def f(g, t):\n    return list(batched_insert_triples(g, t))\n"
    assert _bespoke_markers(_strip_comments(builder_only)) == []


def test_guard_strips_comment_prose():
    """A comment mentioning DELETE WHERE / insert_triples must not trip the scan."""
    planted = "x = 1  # clears the row with a DELETE WHERE, replacing insert_triples()\n"
    assert _bespoke_markers(_strip_comments(planted)) == []


def test_guard_would_fail_for_a_new_unallowlisted_writer():
    """Simulate the deny-by-default decision: a NEW module (not on the allowlist)
    that hand-rolls an instance write is a violation."""
    fake_rel = "api/routes/some_new_writer.py"
    fake_src = 'sparql = f"DELETE {{ GRAPH <{g}> {{ <{s}> ?p ?o }} }} WHERE {{ ... }}"\n'
    marks = _bespoke_markers(_strip_comments(fake_src))
    is_violation = bool(marks) and fake_rel not in _ALLOWLIST
    assert is_violation, "a new hand-rolled instance writer must be denied by default"


def test_guard_would_fail_for_planted_batched_insert_flush():
    """Planted-violation self-test (ONTA-528): a new unallowlisted module that
    flushes via batched_insert_triples + neptune.update is denied by default."""
    fake_rel = "resolver/some_new_flush.py"
    planted = (
        "async def flush(n, g, t):\n"
        "    for s in batched_insert_triples(g, t):\n"
        "        await n.update(s)\n"
    )
    marks = _bespoke_markers(_strip_comments(planted))
    assert "batched_insert_triples+neptune.update" in marks
    assert fake_rel not in _ALLOWLIST
    assert bool(marks) and fake_rel not in _ALLOWLIST
