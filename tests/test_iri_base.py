"""Configurable IRI base (ONTA brand default + env override)."""

from cograph_client.graph.iri import (
    DEFAULT_IRI_BASE,
    ENTITY_URI_PREFIX,
    GRAPH_URI_PREFIX,
    IRI_BASE,
    LEGACY_IRI_BASES,
    TYPE_URI_PREFIX,
)
from cograph_client.graph.ontology_queries import entity_uri, type_uri
from cograph_client.graph.queries import kg_graph_uri, tenant_graph_uri
from cograph_client.nlp.validator import normalize_sparql


def test_default_base_is_onta_branded():
    assert IRI_BASE == DEFAULT_IRI_BASE == "https://graph.onta.sh"
    assert not IRI_BASE.endswith("/")
    assert "cograph.tech" not in IRI_BASE


def test_derived_prefixes_share_base():
    assert TYPE_URI_PREFIX == f"{IRI_BASE}/types/"
    assert ENTITY_URI_PREFIX == f"{IRI_BASE}/entities/"
    assert GRAPH_URI_PREFIX == f"{IRI_BASE}/graphs/"


def test_minters_use_live_base():
    assert entity_uri("City", "San Francisco") == f"{IRI_BASE}/entities/City/San_Francisco"
    assert type_uri("Person") == f"{IRI_BASE}/types/Person"
    assert tenant_graph_uri("demo") == f"{IRI_BASE}/graphs/demo"
    assert kg_graph_uri("demo", "kg1") == f"{IRI_BASE}/graphs/demo/kg/kg1"


def test_normalize_sparql_rewrites_legacy_hosts():
    for legacy in LEGACY_IRI_BASES:
        sparql = f"SELECT ?s WHERE {{ ?s a <{legacy}/types/Film> }}"
        out = normalize_sparql(sparql)
        assert f"<{IRI_BASE}/types/Film>" in out
        assert legacy not in out


def test_legacy_bases_documented():
    assert "https://cograph.tech" in LEGACY_IRI_BASES
    assert "https://omnix.dev" in LEGACY_IRI_BASES


def test_no_runtime_host_literals_outside_allowlist():
    """Deny-by-default: production code must not bake either brand host into
    runtime string literals. Allowed: iri.py (default + legacy list), docs via
    comments, and the prompt template that materializes IRI_BASE at import.
    """
    import pathlib
    import re
    import tokenize
    import io

    root = pathlib.Path(__file__).resolve().parents[1] / "cograph_client"
    allow_files = {
        "iri.py",
        "prompts.py",  # template uses default host then .replace(IRI_BASE)
    }
    # hosts that must not appear as string-literal content outside allowlist
    banned = ("https://cograph.tech", "https://graph.onta.sh")
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.name in allow_files:
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        except tokenize.TokenError:
            continue
        for tok in tokens:
            if tok.type != tokenize.STRING:
                continue
            # unquote roughly
            s = tok.string
            if s.startswith(("f", "F", "r", "R", "b", "B", "u", "U")):
                # strip prefixes
                while s and s[0] in "fFrRbBuU":
                    s = s[1:]
            if len(s) >= 2 and s[0] in "\"'":
                body = s[1:-1] if s[0] * 3 != s[:3] else s[3:-3]
            else:
                body = s
            # skip if it's clearly an f-string expression containing {IRI_BASE}
            if "{IRI_BASE}" in body:
                continue
            # Docstrings / prose examples are multi-line or long — not runtime mint sites.
            if "\n" in body or len(body) > 96:
                continue
            for host in banned:
                if host in body:
                    offenders.append(f"{path.relative_to(root.parent)}:{tok.start[0]}:{host}")
    assert not offenders, (
        "runtime host literals must go through graph.iri — found:\n  "
        + "\n  ".join(offenders[:20])
    )
