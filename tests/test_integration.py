"""Integration test: ingest bookstore.csv and ask 5 questions.

Requires a running SPARQL endpoint (Fuseki or Neptune) and an LLM API key.
Skip with: pytest tests/test_integration.py -k "not integration"

Run with:
    OPENROUTER_API_KEY=sk-or-... pytest tests/test_integration.py -v -s --run-integration
"""

import os
import subprocess
import sys
import time

import pytest

API_URL = os.environ.get("OMNIX_API_URL", "http://localhost:8000")
API_KEY = os.environ.get("OMNIX_API_KEY", "")
KG_NAME = "test-bookstore"
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "examples", "bookstore.csv")


def _headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def _run_cli(*args) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["OMNIX_API_URL"] = API_URL
    if API_KEY:
        env["OMNIX_API_KEY"] = API_KEY
    return subprocess.run(
        [sys.executable, "-m", "cograph.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def pytest_addoption(parser):
    parser.addoption("--run-integration", action="store_true", default=False)


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip = pytest.mark.skip(reason="needs --run-integration flag")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="module")
def ingest_bookstore():
    """Ingest the bookstore CSV once for all tests in this module."""
    # Clear any previous test KG
    result = _run_cli("clear", "--kg", KG_NAME, "-y")
    # Ingest
    result = _run_cli("ingest", CSV_PATH, "--kg", KG_NAME)
    assert result.returncode == 0, f"Ingestion failed: {result.stderr}"
    # Small delay for graph to settle
    time.sleep(2)
    yield
    # Cleanup
    _run_cli("clear", "--kg", KG_NAME, "-y")


def _ask(question: str) -> str:
    """Ask a question via CLI and return the answer."""
    result = _run_cli("ask", question, "--kg", KG_NAME)
    assert result.returncode == 0, f"Ask failed: {result.stderr}"
    return result.stdout.strip()


@pytest.mark.integration
class TestBookstoreIntegration:

    def test_total_count(self, ingest_bookstore):
        answer = _ask("How many books are there in total?")
        assert "20" in answer, f"Expected 20 books, got: {answer}"

    def test_filter_genre(self, ingest_bookstore):
        answer = _ask("How many Dystopian books are there?")
        assert "4" in answer, f"Expected 4 dystopian books, got: {answer}"

    def test_author_query(self, ingest_bookstore):
        answer = _ask("Which books were written by J.R.R. Tolkien?")
        assert "Hobbit" in answer or "Lord" in answer, f"Expected Tolkien books, got: {answer}"

    def test_aggregation(self, ingest_bookstore):
        answer = _ask("What is the most expensive book?")
        assert "Lord of the Rings" in answer or "24.99" in answer, f"Expected LOTR, got: {answer}"

    def test_relationship_join(self, ingest_bookstore):
        answer = _ask("How many different genres are there?")
        # Bookstore has: Classic Fiction, Dystopian, Romance, Coming of Age,
        # Fantasy, Science Fiction, War Fiction, Satire, Historical Fiction = 9
        # Allow some flexibility since the LLM might count differently
        for n in ["8", "9", "10"]:
            if n in answer:
                return
        pytest.fail(f"Expected ~9 genres, got: {answer}")
