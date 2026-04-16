from cograph.nlp.validator import validate_sparql


def test_valid_select():
    ok, err = validate_sparql("SELECT ?s ?p ?o WHERE { ?s ?p ?o }")
    assert ok
    assert err == ""


def test_empty_query():
    ok, err = validate_sparql("")
    assert not ok
    assert "Empty" in err


def test_blocks_insert():
    ok, err = validate_sparql("INSERT DATA { <a> <b> <c> }")
    assert not ok
    assert "INSERT" in err


def test_blocks_delete():
    ok, err = validate_sparql("DELETE WHERE { ?s ?p ?o }")
    assert not ok
    assert "DELETE" in err


def test_blocks_drop():
    ok, err = validate_sparql("DROP GRAPH <http://example.com>")
    assert not ok
    assert "DROP" in err


def test_mismatched_braces():
    ok, err = validate_sparql("SELECT ?s WHERE { ?s ?p ?o")
    assert not ok
    assert "braces" in err.lower()


def test_allows_filter_with_keyword_substring():
    ok, err = validate_sparql(
        "SELECT ?s WHERE { ?s <http://example.com/created> ?o }"
    )
    assert ok


def test_blocks_clear():
    ok, err = validate_sparql("CLEAR GRAPH <http://example.com>")
    assert not ok
    assert "CLEAR" in err
