"""Drift guard (ONTA-400): Layer.PUBLIC may only carry attributes + relationships.

Product rule (founder, ONTA-396): Public is attrs+rels only; skills, functions,
and sources belong on Enhanced (B) or Tenant (C). Enforcement is a hard
**invariant** (``LAYER_A_CONTENT_ENFORCEMENT == "invariant"``), not a lint —
see ``infona_client/graph/layer_content.py``.

Two layers, modelled on ``test_write_path_convergence.py`` /
``test_entity_uri_convergence.py``:

* **Structural** — deny-by-default source + seed scans so a NEW writer or a
  non-empty Public skill seed fails CI without anyone remembering to extend a
  list.
* **Behavioral** — drive the live writers (``register_skill_layer``,
  ``register_function_triple``) and assert they refuse forbidden Public content.

Planted-violation self-tests prove every violation class the guard claims to
catch actually trips the scan / refusal. The guard must NOT fire on the
``registry_layer`` axis (``global_public`` / ``global_enhanced`` on API
sources) — that is a different concept from ontology ``Layer.PUBLIC``.
"""

from __future__ import annotations

import io
import pathlib
import re
import tokenize

import pytest

import infona_client
from infona_client.graph.layer_content import (
    LAYER_A_CONTENT_ENFORCEMENT,
    LAYER_CONTENT_MATRIX,
    ContentKind,
    LayerContentError,
    assert_permits,
    forbidden_kinds,
    is_public_type_uri,
    permits,
)
from infona_client.graph.layers import Layer, layer_type_uri, type_namespace
from infona_client.graph.queries import register_function_triple
from infona_client.skills import TypeSkill, register_skill_layer, reset_skill_layers
from infona_client.skills.registry import global_skills_by_layer

_PKG_ROOT = pathlib.Path(infona_client.__file__).parent
_MATRIX_HOME = "graph/layer_content.py"
_SKILLS_DATA = _PKG_ROOT / "skills" / "data"

# --------------------------------------------------------------------------- #
# Markers (structural)
# --------------------------------------------------------------------------- #
# M1 — a second definition of LAYER_CONTENT_MATRIX outside the home module.
#     Writers and the guard MUST import the one table; a local copy would let
#     them disagree about what Public may carry. Allows a type annotation
#     between the name and ``=`` (``LAYER_CONTENT_MATRIX: Final[...] =``).
_M_MATRIX_DEF = re.compile(r"\bLAYER_CONTENT_MATRIX\s*(?::[^=]+)?\s*=")

# M2 — production code that registers non-empty skills onto Layer.PUBLIC.
#     Empty registration is the reserved-empty seed path and is fine; a call
#     that materialises skill objects for PUBLIC is the violation.
#     Negative lookahead: NOT (optional ws + empty list/tuple + close-paren).
_M_SKILL_PUBLIC = re.compile(
    r"register_skill_layer\s*\(\s*Layer\.PUBLIC\s*,"
    r"(?!\s*(?:\[\s*\]|\(\s*\))\s*\))"
)

# M3 — production function attachment that hardcodes a Public type URI as the
#     attachedTo target (bypassing register_function_triple's refusal).
#     Requires BOTH as quoted string literals so docstring prose
#     (``types/public/<T>`` near "attachedTo" in narrative) and ontology
#     schema writers that only mint types/public/<T> for attrs/rels do NOT trip.
_M_FUNC_PUBLIC_ATTACH = re.compile(
    r"""["']https://graph\.onta\.sh/onto/attachedTo["']"""
    r""".{0,160}?"""
    r"""["']https://graph\.onta\.sh/types/public/"""
    r"|"
    r"""["']https://graph\.onta\.sh/types/public/"""
    r""".{0,160}?"""
    r"""["']https://graph\.onta\.sh/onto/attachedTo["']""",
    re.DOTALL,
)

# Deliberately NOT scanned as a Public-content violation (different axis):
#   registry_layer = "global_public" / "global_enhanced"
# Those strings name the API-source catalog layer, not ontology Layer.PUBLIC.
# See test_guard_ignores_registry_layer_axis.


# Allowlist: modules permitted to mention the markers for a documented reason.
# House style: one-line justification per entry.
_ALLOWLIST: dict[str, str] = {
    # The matrix home — the ONE place LAYER_CONTENT_MATRIX may be defined.
    "graph/layer_content.py": (
        "single definition site for LAYER_CONTENT_MATRIX + ContentKind + "
        "assert_permits; writers and the guard both import it (ONTA-400)."
    ),
}


def _strip_comments(src: str) -> str:
    """Blank out ``#`` COMMENT token spans, preserving line/column structure.

    Keeps string literals (so a real attachedTo URI inside an f-string is still
    scanned) but removes prose comments that mention the forbidden shapes.
    """
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


def _matrix_def_markers(code: str) -> list[str]:
    marks: list[str] = []
    if _M_MATRIX_DEF.search(code):
        marks.append("LAYER_CONTENT_MATRIX = … definition")
    return marks


def _skill_public_markers(code: str) -> list[str]:
    marks: list[str] = []
    if _M_SKILL_PUBLIC.search(code):
        marks.append("register_skill_layer(Layer.PUBLIC, non-empty)")
    return marks


def _func_public_markers(code: str) -> list[str]:
    marks: list[str] = []
    if _M_FUNC_PUBLIC_ATTACH.search(code):
        marks.append("function attachedTo types/public/…")
    return marks


def _public_content_markers(code: str) -> list[str]:
    """All structural markers that indicate forbidden Public content attachment."""
    return _skill_public_markers(code) + _func_public_markers(code)


# --------------------------------------------------------------------------- #
# Positive pins: matrix home + invariant flag
# --------------------------------------------------------------------------- #


def test_matrix_lives_only_in_layer_content():
    """The ONE definition site — writers and this guard both import it."""
    assert LAYER_CONTENT_MATRIX is not None
    assert LAYER_CONTENT_MATRIX[Layer.PUBLIC] == frozenset(
        {ContentKind.ATTRIBUTES, ContentKind.RELATIONSHIPS}
    )
    home = _PKG_ROOT / _MATRIX_HOME
    assert home.is_file()
    src = home.read_text()
    assert "LAYER_CONTENT_MATRIX" in src
    # Import path identity: the name resolves to this module.
    import infona_client.graph.layer_content as lc

    assert lc.LAYER_CONTENT_MATRIX is LAYER_CONTENT_MATRIX
    assert lc.__file__ is not None
    assert pathlib.Path(lc.__file__).resolve() == home.resolve()


def test_a_restriction_is_still_invariant():
    """Wave 0 freeze — ONTA-400 ships a guard, not a lint. Do not flip silently."""
    assert LAYER_A_CONTENT_ENFORCEMENT == "invariant"
    for kind in (ContentKind.SKILLS, ContentKind.FUNCTIONS, ContentKind.SOURCES):
        assert not permits(Layer.PUBLIC, kind)
        assert kind in forbidden_kinds(Layer.PUBLIC)


def test_assert_permits_raises_layer_content_error_for_public_skills():
    with pytest.raises(LayerContentError, match="public layer may not carry skills"):
        assert_permits(Layer.PUBLIC, ContentKind.SKILLS, what="unit")
    # Allowed kinds do not raise.
    assert_permits(Layer.PUBLIC, ContentKind.ATTRIBUTES)
    assert_permits(Layer.ENHANCED, ContentKind.SKILLS)


def test_is_public_type_uri_recognises_namespace_shapes():
    assert is_public_type_uri(layer_type_uri(Layer.PUBLIC, "Person")) is True
    assert is_public_type_uri("public/Person") is True
    assert is_public_type_uri("https://graph.onta.sh/types/public/Org") is True
    # Bare tenant name / enhanced / empty — not Public.
    assert is_public_type_uri("Person") is False
    assert is_public_type_uri("x/Person") is False
    assert is_public_type_uri(layer_type_uri(Layer.ENHANCED, "Person")) is False
    assert is_public_type_uri("") is False


# --------------------------------------------------------------------------- #
# Structural: single matrix definition site
# --------------------------------------------------------------------------- #


def test_no_second_layer_content_matrix_definition():
    """Scan ALL of ``infona_client/`` for a second ``LAYER_CONTENT_MATRIX =``.

    Deny-by-default: a NEW module that copies the table fails here even if
    nobody remembered to converge it onto ``layer_content.py``.
    """
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if rel == _MATRIX_HOME:
            continue
        code = _strip_comments(path.read_text())
        marks = _matrix_def_markers(code)
        if marks:
            violations.append(f"{rel}: {', '.join(marks)}")
    assert not violations, (
        "LAYER_CONTENT_MATRIX re-defined outside graph/layer_content.py. "
        "Import the shared table — never copy it. Offenders:\n  "
        + "\n  ".join(violations)
    )


def test_matrix_home_actually_defines_the_matrix():
    """Sanity: the home file really carries the definition the scan centralises."""
    code = _strip_comments((_PKG_ROOT / _MATRIX_HOME).read_text())
    assert _matrix_def_markers(code), (
        f"{_MATRIX_HOME} must define LAYER_CONTENT_MATRIX (scan home is hollow)"
    )


# --------------------------------------------------------------------------- #
# Structural: no skill / function attachment to Public in production code
# --------------------------------------------------------------------------- #


def test_no_forbidden_public_content_attachment_in_production():
    """Scan production ``infona_client/`` for skills or functions attached to
    the Public type namespace.

    Allowed on Public (NOT flagged here): attributes + relationships —
    ontology schema writers that mint ``types/public/<T>`` for class/attr
    declarations are fine. Forbidden: skills registration onto PUBLIC, and
    function ``attachedTo`` targeting ``types/public/…``.
    """
    violations: list[str] = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        code = _strip_comments(path.read_text())
        marks = _public_content_markers(code)
        if marks:
            violations.append(f"{rel}: {', '.join(marks)}")
    assert not violations, (
        "Forbidden content attached to Layer.PUBLIC / types/public/… found in "
        "production code. Public is attributes + relationships only "
        "(LAYER_CONTENT_MATRIX). Route skills/functions to Enhanced or Tenant, "
        "or — if the module legitimately needs a marker for a documented "
        "reason — add it to _ALLOWLIST with a one-line justification. "
        "Offenders:\n  " + "\n  ".join(violations)
    )


def test_allowlist_entries_are_live():
    """Every allowlist entry must still exist; stale entries hide surface area."""
    stale: list[str] = []
    for rel, reason in _ALLOWLIST.items():
        path = _PKG_ROOT / rel
        if not path.exists():
            stale.append(f"{rel} (file missing)")
            continue
        if not reason.strip():
            stale.append(f"{rel} (empty justification)")
    assert not stale, "Stale layer-content allowlist entries:\n  " + "\n  ".join(stale)


# --------------------------------------------------------------------------- #
# Structural: OSS skill seed is reserved empty
# --------------------------------------------------------------------------- #


def test_oss_skill_seed_is_reserved_empty():
    """``skills/data/`` must not ship skill markdown — Public cannot carry skills.

    Only ``README.md`` (and non-skill files) may live here. A new
    ``data/<Type>/<slug>.md`` fails CI; runtime also refuses a non-empty seed
    inside ``global_skills_by_layer``.
    """
    if not _SKILLS_DATA.is_dir():
        return  # missing dir == empty seed, supported
    skill_files = sorted(
        p.relative_to(_SKILLS_DATA).as_posix()
        for p in _SKILLS_DATA.rglob("*.md")
        if p.name.casefold() != "readme.md"
    )
    assert skill_files == [], (
        "OSS skills seed must stay empty (Public = attrs+rels only, ONTA-400). "
        "Move curated skills to register_skill_layer(Layer.ENHANCED, …). "
        f"Found: {skill_files}"
    )


# --------------------------------------------------------------------------- #
# Behavioral: runtime refusals
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _clean_skill_layers():
    reset_skill_layers()
    yield
    reset_skill_layers()


def _skill(slug: str = "notes", type_name: str = "Person", **kw) -> TypeSkill:
    return TypeSkill(
        slug=slug,
        type_name=type_name,
        body=kw.pop("body", "Some guidance about this type."),
        layer=kw.pop("layer", Layer.ENHANCED),
        tenant_id=kw.pop("tenant_id", None),
        **kw,
    )


def test_register_skill_layer_refuses_nonempty_public():
    """Public may not carry skills — non-empty registration is a hard error."""
    with pytest.raises(LayerContentError, match="may not carry skills"):
        register_skill_layer(Layer.PUBLIC, [_skill()])


def test_register_skill_layer_allows_empty_public_registration():
    """Reserved-empty seed path: empty PUBLIC registration is a no-op, not an error."""
    register_skill_layer(Layer.PUBLIC, [])
    # Nothing landed in the registry for PUBLIC.
    by_layer = global_skills_by_layer()
    assert by_layer.get(Layer.PUBLIC, []) == []


def test_register_skill_layer_still_accepts_enhanced():
    register_skill_layer(Layer.ENHANCED, [_skill(slug="curated")])
    got = global_skills_by_layer()[Layer.ENHANCED]
    assert [s.slug for s in got] == ["curated"]
    assert got[0].layer is Layer.ENHANCED


def test_register_skill_layer_still_refuses_tenant():
    with pytest.raises(ValueError, match="GLOBAL layers only"):
        register_skill_layer(Layer.TENANT, [_skill(layer=Layer.TENANT, tenant_id="t1")])


def test_register_function_triple_refuses_public_type_uri():
    """Attaching a function to a Public-namespace type is refused at the writer."""
    public_uri = layer_type_uri(Layer.PUBLIC, "Place")
    with pytest.raises(LayerContentError, match="may not carry functions"):
        register_function_triple(
            "https://graph.onta.sh/graphs/global/public",
            entity_type=public_uri,
            function_name="f",
            endpoint_url="https://fn/a",
        )


def test_register_function_triple_refuses_path_shaped_public_entity_type():
    """entity_type='public/Place' mints types/public/Place — must refuse."""
    with pytest.raises(LayerContentError, match="may not carry functions"):
        register_function_triple(
            "https://graph.onta.sh/graphs/t1",
            entity_type="public/Place",
            function_name="f",
            endpoint_url="https://fn/a",
        )


def test_register_function_triple_still_accepts_tenant_type():
    """Bare entity_type mints the tenant namespace — permitted (functions on C)."""
    sparql = register_function_triple(
        "https://graph.onta.sh/graphs/t1",
        entity_type="Place",
        function_name="calculate_distance",
        endpoint_url="https://api.example.com/distance",
        description="Calculate distance between places",
    )
    assert "INSERT DATA" in sparql
    assert "graph.onta.sh/functions/calculate_distance" in sparql
    assert "graph.onta.sh/types/Place" in sparql
    # Must NOT have written a Public type URI.
    assert type_namespace(Layer.PUBLIC) not in sparql


def test_register_function_triple_accepts_enhanced_layer():
    """ONTA-399: Enhanced (layer B) may carry functions; URI + graph are qualified."""
    from infona_client.graph.layers import enhanced_graph_uri, layer_type_uri

    sparql = register_function_triple(
        "https://graph.onta.sh/graphs/t1",  # overridden for Enhanced
        entity_type="Place",
        function_name="premium_distance",
        endpoint_url="https://api.example.com/distance",
        layer=Layer.ENHANCED,
    )
    assert layer_type_uri(Layer.ENHANCED, "Place") in sparql
    assert enhanced_graph_uri() in sparql
    assert "graph.onta.sh/types/Place>" not in sparql  # bare tenant subject absent
    assert type_namespace(Layer.PUBLIC) not in sparql


# --------------------------------------------------------------------------- #
# Guard self-tests: the scan actually catches planted violations
# --------------------------------------------------------------------------- #


def test_guard_flags_planted_matrix_redefinition():
    planted = "LAYER_CONTENT_MATRIX = {Layer.PUBLIC: frozenset()}\n"
    assert "LAYER_CONTENT_MATRIX = … definition" in _matrix_def_markers(
        _strip_comments(planted)
    )


def test_guard_flags_planted_public_skill_registration():
    planted = (
        "register_skill_layer(Layer.PUBLIC, "
        "[TypeSkill(slug='x', type_name='T', body='b')])\n"
    )
    assert "register_skill_layer(Layer.PUBLIC, non-empty)" in _skill_public_markers(
        _strip_comments(planted)
    )


def test_guard_flags_planted_function_attach_to_public():
    planted = (
        'triples = [(func, "https://graph.onta.sh/onto/attachedTo", '
        '"https://graph.onta.sh/types/public/Person")]\n'
    )
    assert "function attachedTo types/public/…" in _func_public_markers(
        _strip_comments(planted)
    )


def test_guard_ignores_empty_public_skill_registration():
    """Empty list is the reserved-empty seed path — not a content violation."""
    for planted in (
        "register_skill_layer(Layer.PUBLIC, [])\n",
        "register_skill_layer(Layer.PUBLIC, ())\n",
    ):
        assert _skill_public_markers(_strip_comments(planted)) == [], planted


def test_guard_ignores_registry_layer_axis():
    """``registry_layer='global_public'`` is a DIFFERENT axis from ontology
    Layer.PUBLIC — the API-source catalog, not Public content attachment.

    A planted source row that only carries the registry_layer field must NOT
    trip the Public-content markers (ONTA-400 acceptance criterion).
    """
    planted = '''
spec = ApiSourceSpec(
    slug="nppes",
    title="NPPES",
    layer="global_public",
    registry_layer="global_public",
)
assert src.registry_layer == "global_enhanced"
payload = {"registry_layer": "global_public", "entity_kinds": ["Person"]}
'''
    code = _strip_comments(planted)
    assert _public_content_markers(code) == []
    assert _matrix_def_markers(code) == []


def test_guard_ignores_public_type_uri_for_allowed_schema():
    """Minting types/public/<T> for class/attr declarations is ALLOWED
    (attributes + relationships). The function-attach marker requires
    ``attachedTo`` nearby, so a pure schema URI does not trip it.
    """
    planted = (
        'uri = layer_type_uri(Layer.PUBLIC, "Person")\n'
        'attr = "https://graph.onta.sh/types/public/Person/attrs/email"\n'
        'pub_ns = "https://graph.onta.sh/types/public/"\n'
    )
    assert _func_public_markers(_strip_comments(planted)) == []


def test_guard_ignores_comment_only_mentions():
    planted = (
        "x = 1  # register_skill_layer(Layer.PUBLIC, [s]) + attachedTo types/public/\n"
    )
    code = _strip_comments(planted)
    assert _skill_public_markers(code) == []
    assert _func_public_markers(code) == []


def test_guard_would_fail_for_a_new_unconverged_writer():
    """Simulate deny-by-default: a NEW production module that attaches a
    function to types/public/ is a violation outside the allowlist.
    """
    fake_rel = "resolver/some_new_public_fn_writer.py"
    fake_src = (
        't = "https://graph.onta.sh/types/public/Place"\n'
        'triples = [(f, "https://graph.onta.sh/onto/attachedTo", t)]\n'
    )
    marks = _public_content_markers(_strip_comments(fake_src))
    assert marks, "planted function-on-public must be detected"
    assert fake_rel not in _ALLOWLIST
