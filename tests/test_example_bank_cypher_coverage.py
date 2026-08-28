"""ONTA-539: committed bank has non-trivial Cypher few-shots (hermetic).

Success criteria covered here:
  * Shipped bank has Cypher covering required ADR 0013 shapes
  * Open-data / synthetic only — no spider-bench / eval-mh pollution
  * Seed table itself is free of benchmark KGs
"""

from __future__ import annotations

import json

from infona_client.nlp.cypher_example_seeds import (
    CYPHER_SEEDS,
    REQUIRED_CYPHER_SHAPES,
    SHAPE_RELATED_HOP_LITERAL,
    bank_cypher_shape_coverage,
    seed_shapes_present,
)
from infona_client.nlp.example_bank import (
    DEFAULT_BANK_PATH,
    is_benchmark_kg,
)


def test_seed_table_covers_required_shapes():
    present = seed_shapes_present()
    missing = REQUIRED_CYPHER_SHAPES - present
    assert not missing, f"seed table missing shapes: {sorted(missing)}"


def test_seed_table_has_no_benchmark_kgs():
    for seed in CYPHER_SEEDS:
        assert not is_benchmark_kg(seed["kg_name"]), seed
        assert (seed.get("cypher") or "").strip()
        # No SPARQL body in the cypher field.
        cy = seed["cypher"]
        assert "SELECT" not in cy.upper() or "MATCH" in cy.upper()
        assert "FROM <" not in cy
        assert "PREFIX" not in cy.upper()


def test_shipped_bank_has_cypher_shape_coverage():
    assert DEFAULT_BANK_PATH.exists(), f"missing bank at {DEFAULT_BANK_PATH}"
    coverage = bank_cypher_shape_coverage(DEFAULT_BANK_PATH)
    missing = REQUIRED_CYPHER_SHAPES - coverage
    assert not missing, (
        f"shipped bank missing Cypher shapes {sorted(missing)}; "
        f"have {sorted(coverage)}. Rebuild with: "
        f"python -m infona_client.nlp.cypher_example_seeds"
    )


def test_shipped_bank_cypher_rows_nonempty_and_clean():
    rows = [
        json.loads(line)
        for line in DEFAULT_BANK_PATH.read_text().splitlines()
        if line.strip()
    ]
    cypher_rows = [r for r in rows if (r.get("cypher") or "").strip()]
    assert len(cypher_rows) >= len(REQUIRED_CYPHER_SHAPES), (
        f"expected at least {len(REQUIRED_CYPHER_SHAPES)} cypher rows, got {len(cypher_rows)}"
    )
    for r in cypher_rows:
        assert not is_benchmark_kg(r.get("kg_name") or ""), r.get("kg_name")
        cy = r["cypher"]
        assert "MATCH" in cy.upper()
        # Parameterized isolation — never hardcode a tenant/kg in committed seeds.
        assert "$tenant_id" in cy
        assert "$kg" in cy
        assert "demo-tenant" not in cy
        # Not a SPARQL body.
        assert "FROM <" not in cy
        assert "PREFIX " not in cy.upper()


def test_hop_literal_seeds_filter_literal_on_related_type():
    """T3 teaching body: Assertion hop, then literal on to_e — not from_e."""
    rows = [s for s in CYPHER_SEEDS if s["shape"] == SHAPE_RELATED_HOP_LITERAL]
    assert rows, "expected related_hop_literal_filter seeds"
    for seed in rows:
        cy = seed["cypher"]
        assert "[:SUBJECT]" in cy
        assert "[:OBJECT]" in cy
        assert "[:PREDICATE]" in cy
        assert "to_e[$prop_key]" in cy
        assert "subject_id: to_e.id" in cy
        assert "$from_types" in cy and "$to_types" in cy
        assert "$prop_value" in cy
        assert "from_e[$prop_key]" not in cy
        from infona_client.nlp.pipeline_helpers import _cypher_invented_rel_types

        assert _cypher_invented_rel_types(cy) == []


def test_shipped_bank_has_no_benchmark_rows():
    """Pollution guard still holds after Cypher seed merge (ONTA-449 + ONTA-539)."""
    for line in DEFAULT_BANK_PATH.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        assert not is_benchmark_kg(row.get("kg_name") or ""), row.get("kg_name")
