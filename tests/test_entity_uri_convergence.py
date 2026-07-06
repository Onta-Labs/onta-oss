"""Drift guard: every entity NODE URI is minted through the ONE shared primitive
``graph/ontology_queries.entity_uri`` (and its ``_safe_id`` slugger). No rail may
re-define the id sanitizer or hand-build the ``…/entities/<Type>/<id>`` IRI inline.

The bug this prevents: discovery (resolver/schema_resolver), CSV/JSON ingestion
(resolver/csv_resolver), enrichment (enrichment/executor), and normalization
(normalization/execute) each used to carry their OWN copy of the ``_safe_id``
character class and their OWN inline ``f"…/entities/{T}/{_safe_id(id)}"``. Three
byte-identical sanitizers + five inline constructions is a silent
data-orphaning risk: the moment one copy's slug rule drifts, the same real-world
thing mints under TWO different URIs and the shared node splits in half. Modelled
on ``test_write_path_convergence.py`` — a deny-by-default source scan plus positive
assertions that the converged sites route through the shared minter.

Three markers, scanned across ALL of ``cograph_client/`` and allowed in exactly ONE
file (the minter's home):
  - **MS** — the id-sanitizer character class ``[^a-zA-Z0-9_-]`` (any
    re-implementation of ``_safe_id`` / a ``_slug`` twin, whatever it is named).
  - **ME** — an inline mint of the canonical node IRI with a LITERAL prefix: an
    f-string building ``entities/{Type}/{…(…)}`` (the prefix + a sanitizer CALL).
    Requiring the call ``(`` keeps prose like a ``…/entities/{TypeName}/{slug}``
    docstring — which has no call — from tripping the scan.
  - **ME2** — the same mint built from a local ``ENTITY_URI_PREFIX`` CONSTANT
    (``f"{ENTITY_URI_PREFIX}{Type}/{…}"``) — the shape a per-rail copy grows into
    (normalization's old ``_atom_uri``; #124's ``_node_uri_value`` re-grew it).
    Parse sites (``startswith(ENTITY_URI_PREFIX)``, ``[len(ENTITY_URI_PREFIX):]``,
    ``"{ENTITY_URI_PREFIX}"`` in a SPARQL filter) never write ``ENTITY_URI_PREFIX}{``
    and so don't trip it.
"""

import inspect
import io
import pathlib
import re
import tokenize

import cograph_client
import cograph_client.enrichment.executor as executor_mod
import cograph_client.normalization.execute as normalization_mod
import cograph_client.resolver.csv_resolver as csv_resolver_mod
import cograph_client.resolver.schema_resolver as schema_resolver_mod
from cograph_client.graph.ontology_queries import _safe_id, entity_uri


def _calls(src: str, name: str) -> bool:
    """True if ``src`` contains a CALL to ``name`` (``name(``) at a word boundary."""
    return re.search(rf"(?<![\w.]){re.escape(name)}\(", src) is not None


# --- Positive: the shared minter is the single source of truth ------------------


def test_entity_uri_and_safe_id_live_in_ontology_queries():
    """Both primitives are defined in the cycle-free home (ontology_queries imports
    nothing from the resolver, so every rail can converge on it)."""
    assert _safe_id.__module__ == "cograph_client.graph.ontology_queries"
    assert entity_uri.__module__ == "cograph_client.graph.ontology_queries"


def test_entity_uri_is_prefix_plus_safe_id():
    """The canonical shape: ``…/entities/<Type>/<_safe_id(id)>`` — kept byte-for-byte
    stable so a pure-mechanical convergence orphans zero data."""
    assert entity_uri("City", "San Francisco") == "https://cograph.tech/entities/City/San_Francisco"
    assert entity_uri("Physician", "p1") == "https://cograph.tech/entities/Physician/p1"
    # Composed exactly as prefix + slug, for any (type, id).
    for t, i in [("City", "San Francisco"), ("Model", "eleven v3"), ("X", "")]:
        assert entity_uri(t, i) == f"https://cograph.tech/entities/{t}/{_safe_id(i)}"


def test_safe_id_slug_rules():
    """The historical slug contract (must not drift — it is the node's identity):
    non-``[A-Za-z0-9_-]`` runs → ``_``, capped at 200, empty → ``unknown``."""
    assert _safe_id("San Francisco") == "San_Francisco"
    assert _safe_id("  a/b?c  ") == "a_b_c"
    assert _safe_id("") == "unknown"
    assert _safe_id("   ") == "unknown"
    assert _safe_id("keep-_09AZ") == "keep-_09AZ"
    assert len(_safe_id("x" * 500)) == 200


# --- Structural tripwire: deny-by-default scan of the whole package --------------

# MS — the id-sanitizer character class. ME — an inline canonical-node-IRI mint
# with a literal ``entities/`` prefix (prefix + a sanitizer call). ME2 — the same
# mint built from a local ``ENTITY_URI_PREFIX`` constant instead of a literal
# (``f"{ENTITY_URI_PREFIX}{type}/{slug(x)}"``) — the exact shape a per-rail copy
# tends to grow (normalization once carried it; #124 re-grew it). All three are
# allowed in exactly ONE file: the minter's home. Anything else is a violation.
# ME2 names the specific canonical entities-namespace constant on purpose: it
# must NOT catch a genuinely separate store-internal id prefix like
# ``RULE_ENTITY_PREFIX`` (normalization rule ids), which does not carry the
# ``ENTITY_URI_PREFIX`` substring.
_MS = re.compile(r"\[\^a-zA-Z0-9_-\]")
_ME = re.compile(r"entities/\{[^}]+\}/\{[^}]*\(")
_ME2 = re.compile(r"ENTITY_URI_PREFIX\}\{")

# The ONLY module permitted to define the sanitizer / mint the IRI inline — it IS
# the shared primitive both markers describe.
_HOME = "graph/ontology_queries.py"

_PKG_ROOT = pathlib.Path(cograph_client.__file__).parent


def _strip_comments(src: str) -> str:
    """Blank out ``#`` COMMENT token spans, preserving structure. Keeps string
    literals (so a real inline mint inside an f-string is still scanned) but
    removes prose comments that mention the IRI shape."""
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


def _mint_markers(code: str) -> list[str]:
    marks = []
    if _MS.search(code):
        marks.append("id-sanitizer char class [^a-zA-Z0-9_-]")
    if _ME.search(code):
        marks.append("inline entities/<Type>/<id> mint")
    if _ME2.search(code):
        marks.append("inline ENTITY_URI_PREFIX mint")
    return marks


def test_no_bespoke_entity_uri_minting_outside_the_shared_home():
    """Scan ALL of ``cograph_client/`` for a re-defined id sanitizer or an inline
    canonical-node-IRI mint, and fail on any hit outside the shared minter's home.

    Deny-by-default: a NEW rail that copies the ``_safe_id`` character class or
    hand-builds ``…/entities/{T}/{_safe_id(id)}`` fails here even if nobody
    remembered to converge it — the way discovery + enrichment each carried their
    own copy before this landed."""
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        marks = _mint_markers(_strip_comments(path.read_text()))
        if marks and rel != _HOME:
            violations.append(f"{rel}: {', '.join(marks)}")
    assert not violations, (
        "Bespoke entity-URI minting found OUTSIDE graph/ontology_queries.py. "
        "Mint entity nodes via graph.ontology_queries.entity_uri (and slug ids via "
        "_safe_id) — never re-implement the sanitizer or hand-build the "
        "…/entities/<Type>/<id> IRI inline (that risks a split shared node). "
        "Offenders:\n  " + "\n  ".join(violations)
    )


def test_shared_home_actually_carries_both_markers():
    """Sanity: the home file really is where both markers live, so the scan above
    is meaningfully centralizing behavior (not just an empty allowlist)."""
    code = _strip_comments((_PKG_ROOT / _HOME).read_text())
    marks = _mint_markers(code)
    assert "id-sanitizer char class [^a-zA-Z0-9_-]" in marks
    assert "inline entities/<Type>/<id> mint" in marks


# --- Structural: the converged sites route through the shared minter -------------


def test_schema_resolver_mints_via_shared_entity_uri():
    """Discovery mints entity URIs via the shared ``entity_uri`` (imported as
    ``_entity_uri``) and no longer defines its own ``_safe_id``."""
    src = inspect.getsource(schema_resolver_mod)
    assert "entity_uri as _entity_uri" in src, (
        "schema_resolver must import the shared minter (entity_uri as _entity_uri)"
    )
    assert _calls(src, "_entity_uri"), "schema_resolver must mint via the shared _entity_uri"
    assert "def _safe_id" not in src, (
        "schema_resolver reintroduced a local _safe_id — use graph.ontology_queries._safe_id"
    )


def test_enrichment_node_linking_uses_shared_entity_uri():
    """Enrichment's node-linking mints the target via the shared ``entity_uri`` and
    NOT via a cross-module import of the resolver's ``_safe_id`` (the old coupling)."""
    src = inspect.getsource(executor_mod)
    assert "entity_uri as _entity_uri" in src
    assert _calls(src, "_entity_uri"), "enrichment must mint node targets via the shared _entity_uri"
    assert "from cograph_client.resolver.schema_resolver import _safe_id" not in src, (
        "enrichment reintroduced the cross-module _safe_id import — use "
        "graph.ontology_queries.entity_uri"
    )


def test_csv_resolver_imports_shared_safe_id():
    """CSV/JSON ingestion slugs ids via the shared ``_safe_id`` — no local copy."""
    src = inspect.getsource(csv_resolver_mod)
    assert "def _safe_id" not in src, "csv_resolver reintroduced a local _safe_id"
    assert _calls(src, "_safe_id"), "csv_resolver still slugs ids — must call the shared _safe_id"


def test_normalization_atom_uri_uses_shared_entity_uri():
    """Normalization mints atomic-value nodes through the shared ``entity_uri`` so an
    atom's IRI is byte-identical to the composite's (COG-118), with no ``_slug`` twin."""
    src = inspect.getsource(normalization_mod)
    assert _calls(src, "entity_uri"), "normalization _atom_uri must mint via the shared entity_uri"
    assert "def _slug" not in src, (
        "normalization reintroduced a _slug id-sanitizer twin — mint via "
        "graph.ontology_queries.entity_uri"
    )


# --- Guard self-tests: the scan actually catches planted violations -------------


def test_guard_flags_planted_inline_mint():
    planted = 'uri = f"https://cograph.tech/entities/{t}/{_safe_id(raw)}"\n'
    assert "inline entities/<Type>/<id> mint" in _mint_markers(_strip_comments(planted))


def test_guard_flags_planted_sanitizer():
    planted = 'safe = re.sub(r"[^a-zA-Z0-9_-]", "_", raw.strip())\n'
    assert "id-sanitizer char class [^a-zA-Z0-9_-]" in _mint_markers(_strip_comments(planted))


def test_guard_flags_planted_prefix_constant_mint():
    """The prefix-constant mint form (normalization's old ``_atom_uri`` / #124's
    ``_node_uri_value``) — ``f"{ENTITY_URI_PREFIX}{t}/{_slug(x)}"`` — is caught by
    ME2 even though its literal has no ``entities/`` substring for ME to see."""
    planted = 'return f"{ENTITY_URI_PREFIX}{target_type}/{_slug(value)}"\n'
    assert "inline ENTITY_URI_PREFIX mint" in _mint_markers(_strip_comments(planted))


def test_guard_ignores_separate_rule_id_prefix():
    """A genuinely separate store-internal id prefix (RULE_ENTITY_PREFIX for
    normalization rule resources) must NOT be caught by ME2 — it does not carry
    the canonical ENTITY_URI_PREFIX substring and is a different, single-rail id."""
    planted = 'return f"{RULE_ENTITY_PREFIX}{kg}__{typ}__{make_rule_id(pred)}"\n'
    assert "inline ENTITY_URI_PREFIX mint" not in _mint_markers(_strip_comments(planted))


def test_guard_ignores_iri_shape_prose():
    """A docstring/comment describing the ``…/entities/{Type}/{id}`` shape (no
    sanitizer call) must NOT trip the scan — only real inline mints do."""
    doc = '    """Entity URIs are .../entities/{TargetType}/{id} -> the type leaf."""\n'
    comment = "    x = 1  # keyed …/entities/{TypeName}/{slug}; pull the type\n"
    assert _mint_markers(_strip_comments(doc)) == []
    assert _mint_markers(_strip_comments(comment)) == []


def test_guard_ignores_entity_uri_prefix_parse_and_startswith():
    """Reading/parsing entity URIs (the ENTITY_URI_PREFIX constant, a startswith
    check, a REPLACE regex) is not minting and must not trip the scan."""
    for planted in (
        'ENTITY_URI_PREFIX = "https://cograph.tech/entities/"\n',
        'if uri.startswith("https://cograph.tech/entities/"):\n    pass\n',
        'q = \'BIND(REPLACE(STR(?o), "^.*/entities/([^/]+)/.*$", "$1") AS ?t)\'\n',
    ):
        assert _mint_markers(_strip_comments(planted)) == [], planted


def test_guard_would_fail_for_a_new_unconverged_rail():
    """Simulate deny-by-default: a NEW module (not the home) that hand-builds the
    IRI inline is a violation."""
    fake_rel = "resolver/some_new_rail.py"
    fake_src = 'target = f"https://cograph.tech/entities/{typ}/{_safe_id(v)}"\n'
    marks = _mint_markers(_strip_comments(fake_src))
    assert bool(marks) and fake_rel != _HOME
