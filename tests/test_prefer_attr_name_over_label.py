"""Display-name rewrite: prefer types/<T>/attrs/name over rdfs:label (Eval-MH RCA)."""

from cograph_client.graph.iri import IRI_BASE
from cograph_client.nlp.pipeline import _prefer_attr_name_over_rdfs_label


def test_rdfs_label_rewritten_to_type_attrs_name_when_type_present():
    sparql = f"""
    SELECT ?eventName ?injuredCount WHERE {{
      ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
      ?event <{IRI_BASE}/types/Event/attrs/injured_count> ?injuredCount .
    }}
    """
    out = _prefer_attr_name_over_rdfs_label(sparql)
    assert f"{IRI_BASE}/types/Event/attrs/name" in out
    assert "rdf-schema#label" not in out


def test_no_rewrite_when_attrs_name_already_used():
    sparql = f"""
    SELECT ?n ?lab WHERE {{
      ?e <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Person> .
      ?e <{IRI_BASE}/types/Person/attrs/name> ?n .
      ?e <http://www.w3.org/2000/01/rdf-schema#label> ?lab .
    }}
    """
    out = _prefer_attr_name_over_rdfs_label(sparql)
    assert "rdf-schema#label" in out
    assert out.count(f"{IRI_BASE}/types/Person/attrs/name") == 1


def test_no_op_without_label():
    sparql = f"SELECT ?n WHERE {{ ?e <{IRI_BASE}/types/Person/attrs/name> ?n }}"
    assert _prefer_attr_name_over_rdfs_label(sparql) == sparql
