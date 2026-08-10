"""Block index for entity resolution candidate lookup (SPARQL + GraphStore).

Block keys are stored as literal-valued facts next to each entity:

    <entity_uri> er:blockKey "email_local:johnsmith" .
    <entity_uri> er:blockKey "lastname3_phone4:smi5506" .

Normalized signals are also persisted alongside so a candidate can be
scored without a second round-trip to fetch attributes:

    <entity_uri> er:erSignal_email "john.smith@gmail.com" .
    <entity_uri> er:erSignal_phone "+12005551234" .
    ...

**Dual-backend (Neo4j migration):**

* Neptune path — :class:`SparqlBlocker` (SPARQL SELECT over the instance graph).
* Store path — :class:`GraphStoreBlocker` (Assertion / GraphSession reads when
  ``INFONA_GRAPH_BACKEND=neo4j`` or an explicit store is supplied).

Index **writes** still go through ``index_triples`` → shared ``insert_facts``
(store maps ER leaves to literal Assertions; Neptune keeps RDF triples).

This denormalization costs ~5 facts per ER-enabled entity. The payoff is
one query per ingest row instead of N.
"""


from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from infona_client.graph.iri import ER_NS
from infona_client.resolver.er.types import BlockKey, NormalizedSignals

if TYPE_CHECKING:
    from infona_client.graph.store import GraphSession, GraphStore

BLOCK_KEY_PRED = f"<{ER_NS}blockKey>"
SIGNAL_PRED_PREFIX = f"<{ER_NS}erSignal_"
BLOCK_KEY_LEAF = "blockKey"
SIGNAL_LEAF_PREFIX = "erSignal_"

# Maximum candidates a single block lookup may return. Anything more than
# this is a sign of a degenerate block key (e.g., a phone number used by
# many fake records) — bail rather than spend the scoring budget.
MAX_CANDIDATES = 50


# ---------------------------------------------------------------------------
# Soundex — tiny stdlib implementation (American Soundex, 4-char output)
# ---------------------------------------------------------------------------


_SOUNDEX_MAP = str.maketrans({
    "b": "1", "f": "1", "p": "1", "v": "1",
    "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
    "d": "3", "t": "3",
    "l": "4",
    "m": "5", "n": "5",
    "r": "6",
})


def soundex(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    first = s[0]
    coded = s.translate(_SOUNDEX_MAP)
    # Drop adjacent duplicates
    dedup: list[str] = []
    prev = ""
    for c in coded[1:]:
        if c != prev and c.isdigit():
            dedup.append(c)
        prev = c if c.isdigit() else prev
    return (first.upper() + "".join(dedup) + "000")[:4]


# ---------------------------------------------------------------------------
# Block-key generation
# ---------------------------------------------------------------------------


def generate_block_keys(normalized: NormalizedSignals) -> list[BlockKey]:
    """Emit all blocking strategies for a normalized signal bundle.

    Multiple keys per entity; a candidate matches if it shares ANY key.
    """
    keys: list[BlockKey] = []

    # Strategy 1: email local part — strongest single signal
    if normalized.email_local:
        keys.append(BlockKey("email_local", normalized.email_local))

    # Strategy 2: last-name prefix + phone last 4 — handles missing email
    if normalized.name_tokens and normalized.phone_e164:
        last = normalized.name_tokens[-1] if normalized.name_tokens else ""
        if len(last) >= 3:
            phone_last4 = normalized.phone_e164[-4:]
            keys.append(BlockKey("lastname3_phone4", f"{last[:3]}{phone_last4}"))

    # Strategy 3: soundex(last) + first initial — handles name typos
    if normalized.name_tokens and len(normalized.name_tokens) >= 2:
        first = normalized.name_tokens[0]
        last = normalized.name_tokens[-1]
        if first and last:
            keys.append(BlockKey("soundex_finit", f"{soundex(last)}{first[0]}"))

    # Strategy 4: dob + last-name prefix — handles same-name siblings, etc.
    if normalized.dob_iso and normalized.name_tokens:
        last = normalized.name_tokens[-1]
        if len(last) >= 3:
            keys.append(BlockKey("dob_lname", f"{normalized.dob_iso}_{last[:3]}"))

    # Strategy 5: org-friendly name core (OSS dogfood S4).
    # After legal-suffix strip, "Acme Corp" / "ACME Corporation" both normalize
    # to a single content token "acme". Person-shaped soundex_finit needs ≥2
    # tokens, so pure brand names never co-blocked without these keys.
    # name_core = compact cleaned name (order-preserving); soundex_core =
    # soundex of the first content token (tolerant of minor brand typos).
    if normalized.name:
        ordered = [t for t in normalized.name.split() if t]
        if ordered:
            compact = "".join(ordered)[:24]
            if len(compact) >= 2:
                keys.append(BlockKey("name_core", compact))
            sx = soundex(ordered[0])
            if sx:
                keys.append(BlockKey("soundex_core", sx))

    return keys


# ---------------------------------------------------------------------------
# SPARQL escape (very narrow — block-key values are alphanumeric+colon)
# ---------------------------------------------------------------------------


def _quote_literal(s: str) -> str:
    """Quote an arbitrary string as a SPARQL 1.1 string literal.

    Per the SPARQL grammar (STRING_LITERAL2), inside double-quoted literals
    we escape: backslash, double-quote, line feed, carriage return, tab.
    Everything else is allowed verbatim — including spaces, colons, dots,
    and Unicode letters.

    Earlier versions of this function stripped unsafe chars assuming all
    callers passed alphanumeric+colon block-key values. Signal values
    (names, emails, addresses) routinely contain spaces and other normal
    characters, so stripping silently mangled them at index time.
    """
    escaped = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# GraphStoreBlocker — Assertion / GraphSession path (Neo4j / Memory)
# ---------------------------------------------------------------------------


def _property_leaf_from_id(property_id: str | None) -> str:
    if not property_id:
        return ""
    return str(property_id).rstrip("/").rsplit("/", 1)[-1]


def _signals_from_assertion_rows(
    rows_by_entity: dict[str, list[dict[str, Any]]],
) -> dict[str, NormalizedSignals]:
    """Reassemble Assertion rows into NormalizedSignals per entity.

    Accepts either ``property_id`` IRIs ending in ``erSignal_*`` or a bare
    ``property_name`` / ``key`` leaf.
    """
    per_entity: dict[str, dict[str, list[str]]] = {}
    for uri, rows in rows_by_entity.items():
        sig_lists: dict[str, list[str]] = per_entity.setdefault(uri, {})
        for row in rows:
            leaf = (
                row.get("property_name")
                or row.get("key")
                or _property_leaf_from_id(row.get("property_id"))
            )
            if not leaf or not str(leaf).startswith(SIGNAL_LEAF_PREFIX):
                continue
            signal = str(leaf)[len(SIGNAL_LEAF_PREFIX) :]
            val = row.get("literal_value")
            if val is None:
                continue
            val_s = str(val)
            bucket = sig_lists.setdefault(signal, [])
            if val_s not in bucket:
                bucket.append(val_s)
    return _signal_maps_to_normalized(per_entity)


def _signal_maps_to_normalized(
    per_entity: dict[str, dict[str, list[str]]],
) -> dict[str, NormalizedSignals]:
    """Shared reassembly used by SPARQL bindings + GraphStore assertion rows."""
    out: dict[str, NormalizedSignals] = {}
    for uri, sig_map in per_entity.items():
        emails = sig_map.get("email") or []
        email_locals = sig_map.get("email_local") or []
        primary_email = emails[0] if emails else None
        primary_local = email_locals[0] if email_locals else None
        aliases = tuple(emails[1:])
        local_aliases = tuple(email_locals[1:])
        names = sig_map.get("name") or []
        name = names[0] if names else None
        tokens = tuple(name.split()) if name else ()
        addresses = sig_map.get("address") or []
        address = addresses[0] if addresses else None
        addr_tokens = tuple(address.split()) if address else ()
        phones = sig_map.get("phone_e164") or []
        dobs = sig_map.get("dob_iso") or []
        out[uri] = NormalizedSignals(
            name=name,
            name_tokens=tokens,
            email=primary_email,
            email_local=primary_local,
            email_aliases=aliases,
            email_locals=(primary_local,) + local_aliases if primary_local else local_aliases,
            phone_e164=phones[0] if phones else None,
            address=address,
            address_tokens=addr_tokens,
            dob_iso=dobs[0] if dobs else None,
        )
    return out


class GraphStoreBlocker:
    """Blocker that reads ER index facts via GraphStore (Memory / Neo4j).

    Expects block keys and signals to have been written as literal Assertions
    (via ``index_triples`` → ``insert_facts`` store path).
    """

    def __init__(self, store: "GraphStore"):
        self._store = store

    @staticmethod
    def block_keys(normalized: NormalizedSignals) -> list[BlockKey]:
        return generate_block_keys(normalized)

    def _session_for_graph(self, instance_graph: str) -> Optional["GraphSession"]:
        from infona_client.graph.queries import parse_kg_graph_uri
        from infona_client.graph.scope import GraphScope

        scope = parse_kg_graph_uri(instance_graph)
        if scope is None:
            return None
        tenant_id, kg = scope
        return self._store.session(GraphScope.for_instance(tenant_id, kg))

    async def candidates_with_signals(
        self,
        instance_graph: str,
        type_uri: str,
        keys: list[BlockKey],
    ) -> dict[str, NormalizedSignals]:
        if not keys:
            return {}
        session = self._session_for_graph(instance_graph)
        if session is None:
            return {}
        key_values = {f"{k.kind}:{k.value}" for k in keys}
        entity_ids = await self._entities_of_type(session, type_uri)
        if not entity_ids:
            return {}

        matched: list[str] = []
        signal_rows: dict[str, list[dict[str, Any]]] = {}
        for eid in entity_ids:
            rows = await self._assertions_for(session, eid)
            if not rows:
                continue
            entity_keys = {
                str(r.get("literal_value"))
                for r in rows
                if _property_leaf_from_id(r.get("property_id")) == BLOCK_KEY_LEAF
                and r.get("literal_value") is not None
            }
            if not entity_keys & key_values:
                continue
            matched.append(eid)
            signal_rows[eid] = rows
            if len(matched) >= MAX_CANDIDATES:
                break
        return _signals_from_assertion_rows(signal_rows)

    async def all_entities_with_signals(
        self,
        instance_graph: str,
        type_uri: str,
    ) -> dict[str, NormalizedSignals]:
        session = self._session_for_graph(instance_graph)
        if session is None:
            return {}
        entity_ids = await self._entities_of_type(session, type_uri)
        signal_rows: dict[str, list[dict[str, Any]]] = {}
        for eid in entity_ids:
            rows = await self._assertions_for(session, eid)
            if rows:
                signal_rows[eid] = rows
        return _signals_from_assertion_rows(signal_rows)

    @staticmethod
    async def _entities_of_type(session: "GraphSession", type_uri: str) -> list[str]:
        native = getattr(session, "read_entities_of_type", None)
        if not callable(native):
            return []
        return list(await native([type_uri]) or [])

    @staticmethod
    async def _assertions_for(
        session: "GraphSession", entity_id: str
    ) -> list[dict[str, Any]]:
        native = getattr(session, "read_assertions_for_subject", None)
        if not callable(native):
            return []
        return list(await native(entity_id) or [])

    @staticmethod
    def index_triples(
        entity_uri: str,
        normalized: NormalizedSignals,
        keys: list[BlockKey],
    ) -> list[tuple[str, str, str]]:
        """Same shape as :meth:`SparqlBlocker.index_triples` (shared writer)."""
        return SparqlBlocker.index_triples(entity_uri, normalized, keys)


# ---------------------------------------------------------------------------
# SparqlBlocker (+ dual-path when GraphStore is active)
# ---------------------------------------------------------------------------


class SparqlBlocker:
    """Neptune SPARQL blocker; dual-paths to GraphStore when neo4j backend is on.

    When ``store`` is passed or ``INFONA_GRAPH_BACKEND=neo4j``, candidate and
    rebuild lookups use :class:`GraphStoreBlocker`. Index triple shape is
    identical for both backends (``insert_facts`` maps ER leaves on store).
    """

    def __init__(self, neptune, store: Optional["GraphStore"] = None):
        self._neptune = neptune
        self._store = store

    def _resolve_store(self) -> Optional["GraphStore"]:
        if self._store is not None:
            return self._store
        try:
            from infona_client.graph.store import resolve_optional_graph_store

            return resolve_optional_graph_store()
        except Exception:  # noqa: BLE001 — ER is best-effort
            return None

    def _store_blocker(self) -> Optional[GraphStoreBlocker]:
        store = self._resolve_store()
        if store is None:
            return None
        return GraphStoreBlocker(store)

    @staticmethod
    def block_keys(normalized: NormalizedSignals) -> list[BlockKey]:
        return generate_block_keys(normalized)

    async def candidates_with_signals(
        self,
        instance_graph: str,
        type_uri: str,
        keys: list[BlockKey],
    ) -> dict[str, NormalizedSignals]:
        """Return candidate URIs that share at least one block key, along
        with their stored NormalizedSignals (denormalized for scoring).

        Empty list of keys → empty dict (no candidates).
        """
        if not keys:
            return {}

        store_blocker = self._store_blocker()
        if store_blocker is not None:
            return await store_blocker.candidates_with_signals(
                instance_graph, type_uri, keys
            )

        key_values = ",".join(_quote_literal(f"{k.kind}:{k.value}") for k in keys)
        # Interpolate placeholders (the historical template never .format()'d
        # them — every Neptune lookup ran with literal "{instance_graph}" etc.).
        sparql = (
            "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>\n"
            f"SELECT DISTINCT ?entity ?p ?o\n"
            f"FROM <{instance_graph}>\n"
            "WHERE {\n"
            f"  ?entity rdf:type <{type_uri}> ;\n"
            f"          {BLOCK_KEY_PRED} ?key .\n"
            f"  FILTER(?key IN ({key_values}))\n"
            "  ?entity ?p ?o .\n"
            f'  FILTER(STRSTARTS(STR(?p), "{ER_NS}erSignal_"))\n'
            "}\n"
            f"LIMIT {MAX_CANDIDATES * 8}\n"
        )
        data = await self._neptune.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        out = _bindings_to_signals(rows)
        # Cap to MAX_CANDIDATES (defensive — degenerate block keys)
        if len(out) > MAX_CANDIDATES:
            return dict(list(out.items())[:MAX_CANDIDATES])
        return out

    async def all_entities_with_signals(
        self,
        instance_graph: str,
        type_uri: str,
    ) -> dict[str, NormalizedSignals]:
        """Return EVERY entity of `type_uri` in the graph with its stored
        NormalizedSignals — no block-key filter, no candidate cap.

        Used by the second-pass `er rebuild` (MOE-22): unlike per-row ingest
        lookups, the rebuild needs the whole population at once so it can
        re-block and collapse intra-batch fragments that ingest couldn't see.
        """
        store_blocker = self._store_blocker()
        if store_blocker is not None:
            return await store_blocker.all_entities_with_signals(
                instance_graph, type_uri
            )

        sparql = f"""
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
SELECT ?entity ?p ?o
FROM <{instance_graph}>
WHERE {{
  ?entity rdf:type <{type_uri}> ;
          ?p ?o .
  FILTER(STRSTARTS(STR(?p), "{ER_NS}erSignal_"))
}}
"""
        data = await self._neptune.query(sparql)
        rows = data.get("results", {}).get("bindings", [])
        return _bindings_to_signals(rows)

    @staticmethod
    def index_triples(
        entity_uri: str,
        normalized: NormalizedSignals,
        keys: list[BlockKey],
    ) -> list[tuple[str, str, str]]:
        """Return (subject, predicate, literal) triples that should be inserted
        into the instance graph to make this entity findable by future ER runs.

        The caller batches these into the existing batched_insert_triples flow
        in schema_resolver — no new SPARQL write path needed.
        """
        # IMPORTANT: do NOT pre-quote literal values here. The downstream
        # SPARQL serializer (graph.queries._escape_value) wraps any non-URI
        # string in "..." and escapes inner quotes. Passing a pre-quoted
        # value here produces a doubly-quoted stored literal like
        # `"\"lastname3_phone4:smi5506\""` (the inner quotes become part of
        # the value), which causes every ER candidate-lookup FILTER to miss.
        # Pass raw strings; the serializer handles quoting.
        triples: list[tuple[str, str, str]] = []
        s = f"<{entity_uri}>"
        # Block keys
        for k in keys:
            triples.append((s, BLOCK_KEY_PRED, f"{k.kind}:{k.value}"))
        # Denormalized signals (for fast scoring on future lookups)
        signal_fields = [
            ("name", normalized.name),
            ("email", normalized.email),
            ("email_local", normalized.email_local),
            ("phone_e164", normalized.phone_e164),
            ("address", normalized.address),
            ("dob_iso", normalized.dob_iso),
        ]
        for name, value in signal_fields:
            if value:
                pred = f"<{ER_NS}erSignal_{name}>"
                triples.append((s, pred, value))
        # Also emit alias emails as erSignal_email so merge-expansion aliases
        # survive GraphStore reassembly (same as multi-value Neptune triples).
        for alias in getattr(normalized, "email_aliases", ()) or ():
            if alias and alias != normalized.email:
                triples.append((s, f"<{ER_NS}erSignal_email>", alias))
        return triples


def _bindings_to_signals(rows: list[dict]) -> dict[str, NormalizedSignals]:
    """Reassemble flat (entity_uri, signal_name, signal_value) SPARQL rows into
    one NormalizedSignals per entity.

    Accumulate values per signal: after a canonical has been merge-expanded one
    or more times, it has multiple erSignal_email / erSignal_email_local triples
    representing its accumulated aliases. Naive overwrite loses every alias
    except the last-written one, which silently breaks transitive matching
    (e.g., PMS+CRM merge contributes alt-email; later Loyalty ingest can't find
    the canonical because the alt-email isn't visible).
    """
    per_entity: dict[str, dict[str, list[str]]] = {}
    for row in rows:
        uri = row["entity"]["value"]
        pred = row["p"]["value"]
        val = row["o"]["value"]
        signal = pred.replace(f"{ER_NS}erSignal_", "")
        # Cross-host ER_NS: strip any …/er/erSignal_ prefix.
        if "/er/erSignal_" in pred and not signal.startswith("erSignal_"):
            signal = pred.rsplit("/er/erSignal_", 1)[-1]
        elif signal.startswith("erSignal_"):
            signal = signal[len("erSignal_") :]
        sig_lists = per_entity.setdefault(uri, {})
        sig_lists.setdefault(signal, [])
        if val not in sig_lists[signal]:
            sig_lists[signal].append(val)

    return _signal_maps_to_normalized(per_entity)
