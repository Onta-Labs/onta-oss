"""Pure-function + hermetic dry-run tests for Neptune → Neo4j ETL mapping.

No live Neptune / Neo4j required. Exercises model B1–B5 mappers used by
``scripts/neptune_to_neo4j_etl.py``.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cograph_client.graph.facts import (
    RESERVED_ENTITY_PROPERTY_KEYS,
    Fact,
    classify_triple,
    sanitize_prop_key,
    sanitize_rel_type,
    triples_to_facts,
)
from cograph_client.graph.iri import IRI_BASE
from cograph_client.graph.labels import RESERVED_SYSTEM_LABELS, sanitize_domain_label
from cograph_client.graph.scope import GraphScopeError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "neptune_to_neo4j_etl.py"
FIXTURE_NT = Path(__file__).resolve().parent / "fixtures" / "neo4j_etl_sample.nt"


def _load_etl_module():
    """Import the ETL script as a module (not installed as a package)."""
    name = "neptune_to_neo4j_etl"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve cls.__module__.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


etl = _load_etl_module()


# ---------------------------------------------------------------------------
# B1–B5 pure sanitizers (shared with product writers)
# ---------------------------------------------------------------------------


def test_b1_sanitize_domain_label():
    assert sanitize_domain_label("Person") == "Person"
    assert sanitize_domain_label("HotelGuest") == "HotelGuest"
    assert sanitize_domain_label("city/town") == "city_town"
    assert sanitize_domain_label("2fa") == "T_2fa"
    with pytest.raises(GraphScopeError, match="reserved"):
        sanitize_domain_label("Entity")
    for reserved in ("OntoType", "ProvEvent", "KgMeta"):
        assert reserved in RESERVED_SYSTEM_LABELS


def test_b1_sanitize_rel_type_upper_snake():
    assert sanitize_rel_type("works_at") == "WORKS_AT"
    assert sanitize_rel_type("city/town") == "CITY_TOWN"
    assert sanitize_rel_type("2fa_link") == "T_2FA_LINK"


def test_b1_sanitize_prop_key():
    assert sanitize_prop_key("email") == "email"
    assert sanitize_prop_key("city/town") == "city_town"
    assert sanitize_prop_key("2fa") == "T_2fa"
    with pytest.raises(GraphScopeError, match="reserved"):
        sanitize_prop_key("tenant_id")
    with pytest.raises(GraphScopeError, match="reserved"):
        sanitize_prop_key("name")  # reserved for display; rdfs:label maps via classify


def test_b2_reserved_entity_property_keys():
    for key in ("id", "tenant_id", "kg", "primary_type", "name", "label", "source"):
        assert key in RESERVED_ENTITY_PROPERTY_KEYS


def test_b3_attrs_type_segment_ignored():
    """Entity-scoped props: same leaf under two types → one key (classify)."""
    subj = f"{IRI_BASE}/entities/Person/alice"
    t1 = classify_triple(
        subj, f"{IRI_BASE}/types/Person/attrs/status", "active"
    )
    t2 = classify_triple(
        subj, f"{IRI_BASE}/types/Guest/attrs/status", "vip"
    )
    assert t1 is not None and t2 is not None
    assert t1.kind == t2.kind == "literal"
    assert t1.key == t2.key == "status"
    assert t1.value == "active"
    assert t2.value == "vip"


def test_b5_subject_id_is_entity_iri_string():
    subj = f"{IRI_BASE}/entities/Person/alice"
    fact = classify_triple(
        subj,
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
        f"{IRI_BASE}/types/Person",
    )
    assert fact is not None
    assert fact.subject_id == subj
    assert fact.kind == "type"
    assert fact.key == "Person"


def test_classify_rdfs_label_to_name_not_label():
    subj = f"{IRI_BASE}/entities/Person/bob"
    fact = classify_triple(
        subj,
        "http://www.w3.org/2000/01/rdf-schema#label",
        "Bob",
    )
    assert fact == Fact(subject_id=subj, kind="literal", key="name", value="Bob")


def test_classify_onto_rel_and_source():
    person = f"{IRI_BASE}/entities/Person/alice"
    org = f"{IRI_BASE}/entities/Organization/acme"
    rel = classify_triple(person, f"{IRI_BASE}/onto/works_at", org)
    src = classify_triple(person, f"{IRI_BASE}/onto/source", "csv")
    assert rel is not None and rel.kind == "rel" and rel.key == "works_at"
    assert rel.value == org
    assert src is not None and src.kind == "literal" and src.key == "source"


def test_classify_skips_attr_meta_companions():
    subj = f"{IRI_BASE}/entities/Person/alice"
    skipped = classify_triple(
        subj,
        f"{IRI_BASE}/attr_meta/Person/email/source_url",
        "https://example.com",
    )
    assert skipped is None


# ---------------------------------------------------------------------------
# ETL module pure parsers / stats
# ---------------------------------------------------------------------------


def test_parse_ntriples_basic():
    text = (
        '<http://ex/s> <http://ex/p> <http://ex/o> .\n'
        '<http://ex/s> <http://ex/p2> "hello" .\n'
        '# comment\n'
        '\n'
    )
    triples = etl.parse_ntriples(text)
    assert triples == [
        ("http://ex/s", "http://ex/p", "http://ex/o"),
        ("http://ex/s", "http://ex/p2", "hello"),
    ]


def test_parse_json_triples_shapes():
    as_objects = [{"s": "a", "p": "b", "o": "c"}]
    as_lists = [["a", "b", "c"]]
    wrapped = {"triples": [{"subject": "a", "predicate": "b", "object": "c"}]}
    assert etl.parse_json_triples(as_objects) == [("a", "b", "c")]
    assert etl.parse_json_triples(as_lists) == [("a", "b", "c")]
    assert etl.parse_json_triples(wrapped) == [("a", "b", "c")]


def test_parse_instance_graph_uri_any_host():
    assert etl.parse_instance_graph_uri(
        "https://cograph.tech/graphs/demo-tenant/kg/bookstore"
    ) == ("demo-tenant", "bookstore")
    assert etl.parse_instance_graph_uri(
        "https://graph.onta.sh/graphs/acme/kg/crm"
    ) == ("acme", "crm")
    assert etl.parse_instance_graph_uri(
        "https://cograph.tech/graphs/demo-tenant"
    ) is None
    assert etl.parse_instance_graph_uri(
        "https://cograph.tech/graphs/demo-tenant/kg/bookstore/provenance"
    ) is None


def test_count_b3_literal_conflicts():
    sid = f"{IRI_BASE}/entities/Person/x"
    facts = [
        Fact(subject_id=sid, kind="literal", key="status", value="vip"),
        Fact(subject_id=sid, kind="literal", key="status", value="active"),
        Fact(subject_id=sid, kind="literal", key="email", value="a@b.c"),
        Fact(subject_id=sid, kind="type", key="Person"),
    ]
    assert etl.count_b3_literal_conflicts(facts) == 1


def test_map_triples_fixture_nt():
    triples = etl.load_triples_from_file(FIXTURE_NT)
    assert len(triples) >= 8
    facts, stats = etl.map_triples(triples)
    assert stats.triples_in == len(triples)
    assert stats.facts_out >= 8
    assert stats.skipped >= 1  # attr_meta companion
    assert stats.kind_counts.get("type", 0) >= 2
    assert stats.kind_counts.get("rel", 0) >= 1
    assert stats.kind_counts.get("literal", 0) >= 4
    assert stats.b3_literal_conflicts >= 1  # status vip then active
    assert stats.subjects >= 2
    kinds = {(f.kind, f.key) for f in facts}
    assert ("type", "Person") in kinds
    assert ("literal", "name") in kinds
    assert ("rel", "works_at") in kinds
    assert ("literal", "source") in kinds


def test_sparql_bindings_to_triples():
    payload = {
        "results": {
            "bindings": [
                {
                    "s": {"type": "uri", "value": "http://s"},
                    "p": {"type": "uri", "value": "http://p"},
                    "o": {"type": "literal", "value": "v"},
                },
                {"s": {"type": "uri", "value": "http://s"}},  # incomplete → skip
            ]
        }
    }
    assert etl.sparql_bindings_to_triples(payload) == [
        ("http://s", "http://p", "v")
    ]


def test_load_json_fixture(tmp_path: Path):
    path = tmp_path / "t.json"
    path.write_text(
        json.dumps(
            [
                {
                    "s": f"{IRI_BASE}/entities/Book/b1",
                    "p": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                    "o": f"{IRI_BASE}/types/Book",
                }
            ]
        ),
        encoding="utf-8",
    )
    triples = etl.load_triples_from_file(path)
    facts = triples_to_facts(triples)
    assert len(facts) == 1
    assert facts[0].kind == "type" and facts[0].key == "Book"


# ---------------------------------------------------------------------------
# Hermetic CLI dry-run (subprocess — no NEO4J / Neptune)
# ---------------------------------------------------------------------------


def test_cli_dry_run_fixture_hermetic():
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()})}
    # Ensure we do not accidentally try to write.
    env.pop("NEO4J_URI", None)
    env.pop("NEO4J_PASSWORD", None)
    env.pop("NEPTUNE_ENDPOINT", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--fixture",
            str(FIXTURE_NT),
            "--tenant",
            "demo-tenant",
            "--kg",
            "bookstore",
            "--dry-run",
            "--json-stats",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "triples_in" in proc.stdout
    assert "facts_out" in proc.stdout
    # Last non-empty line is JSON stats
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    stats = json.loads(lines[-1])
    assert stats["triples_in"] >= 8
    assert stats["facts_out"] >= 8
    assert stats["written_facts"] == 0


def test_cli_write_without_neo4j_fails_closed():
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()})}
    env.pop("NEO4J_URI", None)
    env.pop("NEO4J_PASSWORD", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--fixture",
            str(FIXTURE_NT),
            "--tenant",
            "demo-tenant",
            "--kg",
            "bookstore",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 2
    assert "NEO4J" in proc.stderr or "NEO4J" in proc.stdout
