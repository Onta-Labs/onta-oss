"""Enum-attribute CONTAINS FILTER mismatch detection + ask() zero-row recovery.

Product RCA (Oliver Learning DP live rehearsal, 2026-07-28): NL→SPARQL mapped
"after bladder surgery" onto ``Indication.setting CONTAINS "bladder"`` even though
the ontology listed setting values as ``adjuvant`` / ``metastatic`` / etc. Valid
SPARQL, structurally zero rows → "No matches found."

These tests assert the mechanism (pure detection + ask retry), not a live KG.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from infona_client.nlp.enum_filter import (
    enum_mismatch_feedback,
    impossible_enum_contains,
    parse_enum_attr_values,
)
from infona_client.nlp.pipeline import NLQueryPipeline


ONTOLOGY = """\
Type: Indication — URI: <https://graph.infona.ai/types/Indication>
  Attributes: setting (string) — URI: <https://graph.infona.ai/types/Indication/attrs/setting> [values: "adjuvant", "metastatic", "maintenance"], disease (string) — URI: <https://graph.infona.ai/types/Indication/attrs/disease> [27 unique values], indication_summary (string) — URI: <https://graph.infona.ai/types/Indication/attrs/indication_summary> [104 unique values], label_status (string) — URI: <https://graph.infona.ai/types/Indication/attrs/label_status> [values: "on-label", "withdrawn"]
  Relationships: requires_diagnostic → CompanionDiagnostic — predicate URI: <https://graph.infona.ai/onto/requires_diagnostic>
Type: Drug — URI: <https://graph.infona.ai/types/Drug>
  Attributes: brand_name (string) — URI: <https://graph.infona.ai/types/Drug/attrs/brand_name> [100 unique values]
  Relationships: has_indication → Indication — predicate URI: <https://graph.infona.ai/onto/has_indication>
"""

BAD_SPARQL = """\
SELECT DISTINCT ?requiredTest ?supportingStudy
FROM <https://graph.infona.ai/graphs/t/kg/k>
WHERE {
  ?drug <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <https://graph.infona.ai/types/Drug> .
  ?drug <https://graph.infona.ai/types/Drug/attrs/brand_name> ?brand .
  FILTER(CONTAINS(LCASE(?brand), "tecentriq"))
  ?drug <https://graph.infona.ai/onto/has_indication> ?ind .
  ?ind <https://graph.infona.ai/types/Indication/attrs/setting> ?setting .
  FILTER(CONTAINS(LCASE(?setting), "bladder"))
  OPTIONAL {
    ?ind <https://graph.infona.ai/onto/requires_diagnostic> ?diag .
    ?diag <https://graph.infona.ai/types/CompanionDiagnostic/attrs/name> ?requiredTest .
  }
}
"""

GOOD_SPARQL = """\
SELECT DISTINCT ?requiredTest
FROM <https://graph.infona.ai/graphs/t/kg/k>
WHERE {
  ?drug <https://graph.infona.ai/types/Drug/attrs/brand_name> ?brand .
  FILTER(CONTAINS(LCASE(?brand), "tecentriq"))
  ?drug <https://graph.infona.ai/onto/has_indication> ?ind .
  ?ind <https://graph.infona.ai/types/Indication/attrs/disease> ?disease .
  FILTER(CONTAINS(LCASE(?disease), "bladder"))
  ?ind <https://graph.infona.ai/onto/requires_diagnostic> ?diag .
  ?diag <https://graph.infona.ai/types/CompanionDiagnostic/attrs/name> ?requiredTest .
}
"""

OK_ENUM_SPARQL = """\
SELECT ?s WHERE {
  ?ind <https://graph.infona.ai/types/Indication/attrs/setting> ?setting .
  FILTER(CONTAINS(LCASE(?setting), "adjuvant"))
}
"""


def test_parse_enum_attr_values_closed_list_only():
    enums = parse_enum_attr_values(ONTOLOGY)
    setting = "https://graph.infona.ai/types/Indication/attrs/setting"
    status = "https://graph.infona.ai/types/Indication/attrs/label_status"
    disease = "https://graph.infona.ai/types/Indication/attrs/disease"
    assert setting in enums
    assert "adjuvant" in enums[setting]
    assert status in enums
    # high-cardinality "N unique values" must NOT be treated as closed enum
    assert disease not in enums


def test_parse_enum_attr_values_skips_truncated_sample():
    """vals[:10] + '… (25 total)' is not exhaustive — do not treat as closed enum."""
    onto = (
        'setting (string) — URI: <https://graph.infona.ai/types/Indication/attrs/setting> '
        '[values: "adjuvant", "metastatic", "maintenance", "neoadjuvant", '
        '"first-line", "second-line", "consolidation", "perioperative", '
        '"unresectable", "locally advanced", … (25 total)]'
    )
    assert parse_enum_attr_values(onto) == {}
    # needle that only fails the visible subset must NOT flag when truncated
    sparql = (
        '?i <https://graph.infona.ai/types/Indication/attrs/setting> ?setting .\n'
        'FILTER(CONTAINS(LCASE(?setting), "hidden-value-11"))'
    )
    assert impossible_enum_contains(sparql, onto) == []


def test_impossible_enum_contains_flags_bladder_on_setting():
    bad = impossible_enum_contains(BAD_SPARQL, ONTOLOGY)
    assert len(bad) == 1
    assert bad[0].attr_leaf == "setting"
    assert bad[0].needle == "bladder"
    assert "adjuvant" in bad[0].listed_values


def test_impossible_enum_contains_allows_listed_substring():
    assert impossible_enum_contains(OK_ENUM_SPARQL, ONTOLOGY) == []


def test_impossible_enum_contains_allows_free_text_disease():
    # disease has [27 unique values], not a closed list — never flag
    assert impossible_enum_contains(GOOD_SPARQL, ONTOLOGY) == []


def test_enum_mismatch_feedback_names_attribute_and_needle():
    mismatches = impossible_enum_contains(BAD_SPARQL, ONTOLOGY)
    fb = enum_mismatch_feedback(mismatches, previous_sparql=BAD_SPARQL)
    assert "setting" in fb
    assert "bladder" in fb
    assert "adjuvant" in fb
    assert "zero rows" in fb.lower() or "ZERO rows" in fb


def _rows(vars_, *value_rows) -> dict:
    return {
        "head": {"vars": list(vars_)},
        "results": {
            "bindings": [
                {k: {"type": "literal", "value": v} for k, v in row.items()}
                for row in value_rows
            ]
        },
    }


EMPTY = {"head": {"vars": ["requiredTest"]}, "results": {"bindings": []}}
HIT = _rows(["requiredTest"], {"requiredTest": "FDA-authorized ctDNA MRD test"})


@pytest.mark.asyncio
async def test_ask_retries_on_enum_filter_zero_rows():
    """First SPARQL is enum-mismatched (0 rows); second is free-text disease FILTER (hits)."""
    p = NLQueryPipeline.__new__(NLQueryPipeline)
    p.neptune = AsyncMock()
    p.neptune.query = AsyncMock(side_effect=[EMPTY, HIT])
    p._query_provider = "openrouter"
    p._query_model = "test"
    p._openrouter_key = "k"
    p._cerebras_key = None
    p._aliases_enabled = False
    p._spatial_routing_enabled = False
    p._answer_citations_enabled = False
    p.anthropic = None

    gen_calls: list[dict] = []

    async def fake_gen(question, ontology, graph_uri="", error_feedback="", examples_text="", **kw):
        gen_calls.append({"error_feedback": error_feedback, "attempt": len(gen_calls)})
        if len(gen_calls) == 1:
            return {"sparql": BAD_SPARQL, "explanation": "bad", "functions_needed": []}
        return {"sparql": GOOD_SPARQL, "explanation": "good", "functions_needed": []}

    async def fake_format(bindings, explanation, missing_vars=None, **kw):
        if not bindings:
            return "No results found."
        return "ok: " + bindings[0].get("requiredTest", "")

    async def fake_rephrase(question, bindings):
        if not bindings:
            return "No matches found."
        return "Found " + bindings[0].get("requiredTest", "")

    with (
        patch.object(p, "_fetch_ontology", new=AsyncMock(return_value=ONTOLOGY)),
        patch.object(p, "_generate_sparql", new=fake_gen),
        patch.object(p, "_fix_attribute_uris", side_effect=lambda s, o: s),
        patch.object(p, "_fix_common_sparql_issues", side_effect=lambda s, o, a=None: s),
        patch.object(p, "_ensure_order_by", side_effect=lambda s: s),
        patch.object(p, "_broaden_name_lookup", new=AsyncMock(return_value=None)),
        patch.object(p, "_format_answer", new=fake_format),
        patch.object(p, "_rephrase_via_openrouter", new=fake_rephrase),
        patch("infona_client.nlp.pipeline.get_embedding_service", return_value=None),
    ):
        result = await p.ask(
            "A customer asked about Tecentriq after bladder surgery — is there a required test?",
            graph_uri="https://graph.infona.ai/graphs/t",
            instance_graph="https://graph.infona.ai/graphs/t/kg/k",
        )

    assert len(gen_calls) == 2
    assert "bladder" in (gen_calls[1]["error_feedback"] or "")
    assert "setting" in (gen_calls[1]["error_feedback"] or "")
    assert result.timing.get("enum_filter_mismatch_retry") == 1.0
    assert result.timing.get("rows") == 1.0
    assert "ctDNA" in (result.narrative_answer or result.answer)
    assert "No matches" not in (result.narrative_answer or "")
