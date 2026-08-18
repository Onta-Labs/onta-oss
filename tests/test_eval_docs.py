"""Lock the eval harness docs: Cypher, Python entrypoint, shipped CSVs (ONTA-546)."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _module_doc(rel: str) -> str:
    text = (REPO / rel).read_text()
    start = text.find('"""')
    assert start >= 0, f"{rel} has no module docstring"
    end = text.find('"""', start + 3)
    assert end > start, f"{rel} module docstring is unclosed"
    return text[start + 3 : end]


def test_eval_module_doc_is_python_cypher_entrypoint() -> None:
    doc = _module_doc("infona_client/eval.py")
    assert "run_full_eval" in doc
    assert "from infona_client.eval import" in doc
    assert "Cypher" in doc
    assert "SPARQL" not in doc
    # Do not advertise a phantom CLI as the way to run eval.
    assert "infona eval" not in doc
    assert "@infona-ai/cli" in doc
    assert "examples/trials.csv" in doc
    assert "examples/bookstore.csv" in doc
    assert "listings.csv" not in doc
    assert "restaurants.csv" not in doc


def test_shipped_example_datasets_exist() -> None:
    assert (REPO / "examples" / "trials.csv").is_file()
    assert (REPO / "examples" / "bookstore.csv").is_file()


def test_eval_diagnosis_doc_does_not_sell_sparql_as_product() -> None:
    doc = _module_doc("infona_client/eval_diagnosis.py")
    assert "SPARQL generation is wrong" not in doc
    assert "needs SPARQL UPDATE" not in doc
    assert "Cypher" in doc
