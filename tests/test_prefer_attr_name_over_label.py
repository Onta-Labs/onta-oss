"""Display-name rewrite: prefer types/<T>/attrs/name over rdfs:label (Eval-MH RCA)."""

from infona_client.graph.iri import IRI_BASE
from infona_client.nlp.pipeline import _prefer_attr_name_over_rdfs_label


def test_rdfs_label_rewritten_when_ontology_declares_attrs_name():
    sparql = f"""
    SELECT ?eventName ?injuredCount WHERE {{
      ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
      ?event <{IRI_BASE}/types/Event/attrs/injured_count> ?injuredCount .
    }}
    """
    ontology = f"name (string) — URI: <{IRI_BASE}/types/Event/attrs/name>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert f"{IRI_BASE}/types/Event/attrs/name" in out
    assert "rdf-schema#label" not in out


def test_prefixed_rdfs_label_on_typed_subject_rewritten():
    sparql = f"""
    SELECT ?n WHERE {{
      ?e a <{IRI_BASE}/types/Event> .
      ?e rdfs:label ?n .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Event/attrs/name>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert f"<{IRI_BASE}/types/Event/attrs/name>" in out
    assert "rdfs:label" not in out.lower()


def test_no_rewrite_when_ontology_lacks_attrs_name():
    sparql = f"""
    SELECT ?eventName WHERE {{
      ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Event> — no name attr listed"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out


def test_no_rewrite_on_name_prefix_false_positive():
    """attrs/namespace must not satisfy the attrs/name ontology gate."""
    sparql = f"""
    SELECT ?eventName WHERE {{
      ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Event/attrs/namespace>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out
    assert f"{IRI_BASE}/types/Event/attrs/name>" not in out


def test_no_rewrite_on_name_slug_only():
    sparql = f"""
    SELECT ?n WHERE {{
      ?e a <{IRI_BASE}/types/Event> .
      ?e <http://www.w3.org/2000/01/rdf-schema#label> ?n .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Event/attrs/name_slug>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out


def test_no_rewrite_when_attrs_name_already_used():
    sparql = f"""
    SELECT ?n ?lab WHERE {{
      ?e <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>
        <{IRI_BASE}/types/Person> .
      ?e <{IRI_BASE}/types/Person/attrs/name> ?n .
      ?e <http://www.w3.org/2000/01/rdf-schema#label> ?lab .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Person/attrs/name>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out
    assert out.count(f"{IRI_BASE}/types/Person/attrs/name") == 1


def test_no_rewrite_on_multi_type_query():
    sparql = f"""
    SELECT ?en ?pn WHERE {{
      ?e <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{IRI_BASE}/types/Event> .
      ?p <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> <{IRI_BASE}/types/Person> .
      ?e <http://www.w3.org/2000/01/rdf-schema#label> ?en .
    }}
    """
    ontology = (
        f"URI: <{IRI_BASE}/types/Event/attrs/name> "
        f"URI: <{IRI_BASE}/types/Person/attrs/name>"
    )
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out


def test_no_rewrite_label_on_related_untyped_var():
    """Label on a joined entity must not become the anchor type's attrs/name."""
    sparql = f"""
    SELECT ?venueName ?injured WHERE {{
      ?e a <{IRI_BASE}/types/Event> .
      ?e <{IRI_BASE}/types/Event/attrs/injured_count> ?injured .
      ?e <{IRI_BASE}/onto/venue> ?v .
      ?v <http://www.w3.org/2000/01/rdf-schema#label> ?venueName .
    }}
    """
    ontology = f"URI: <{IRI_BASE}/types/Event/attrs/name>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert "rdf-schema#label" in out
    assert f"{IRI_BASE}/types/Event/attrs/name" not in out


def test_no_rewrite_when_ontology_summary_empty():
    sparql = f"""
    SELECT ?n WHERE {{
      ?e a <{IRI_BASE}/types/Event> .
      ?e <http://www.w3.org/2000/01/rdf-schema#label> ?n .
    }}
    """
    out = _prefer_attr_name_over_rdfs_label(sparql, "")
    assert "rdf-schema#label" in out


def test_no_op_without_label():
    sparql = f"SELECT ?n WHERE {{ ?e <{IRI_BASE}/types/Person/attrs/name> ?n }}"
    assert _prefer_attr_name_over_rdfs_label(sparql, "anything") == sparql


def test_rewrite_after_subclass_closure_path():
    """Production path: Fix 4 rewrites type to <#type>/<#subClassOf>* first."""
    sparql = f"""
    SELECT ?eventName WHERE {{
      ?event <http://www.w3.org/1999/02/22-rdf-syntax-ns#type>/<http://www.w3.org/2000/01/rdf-schema#subClassOf>*
        <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
    }}
    """
    ontology = f"name (string) — URI: <{IRI_BASE}/types/Event/attrs/name>"
    out = _prefer_attr_name_over_rdfs_label(sparql, ontology)
    assert f"{IRI_BASE}/types/Event/attrs/name" in out
    assert "rdf-schema#label" not in out


def test_fix_common_sparql_issues_rewrites_label_on_typed_subject():
    """Integration: Fix 1 → Fix 4 → Fix 7 must still rewrite display labels."""
    from infona_client.nlp.pipeline import NLQueryPipeline

    sparql = f"""
    SELECT ?eventName ?injuredCount WHERE {{
      ?event a <{IRI_BASE}/types/Event> .
      ?event <http://www.w3.org/2000/01/rdf-schema#label> ?eventName .
      ?event <{IRI_BASE}/types/Event/attrs/injured_count> ?injuredCount .
    }}
    """
    ontology = f"name (string) — URI: <{IRI_BASE}/types/Event/attrs/name>"
    out = NLQueryPipeline._fix_common_sparql_issues(sparql, ontology_summary=ontology)
    assert f"{IRI_BASE}/types/Event/attrs/name" in out
    assert "rdf-schema#label" not in out
    # Fix 4 still applied
    assert "subClassOf" in out
