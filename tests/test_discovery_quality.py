"""Discovery quality gate — website policy + near-dup merge + page yield probe."""

from __future__ import annotations

from cograph_client.pipeline.discovery_quality import (
    apply_discovery_quality_gate,
    merge_near_duplicates,
    normalize_entity_key,
    page_looks_like_list,
    page_yield_score,
    registrable_host,
    scrub_website_policy,
)


def test_scrub_website_equals_source_url():
    row = {
        "name": "University of British Columbia",
        "website": "https://en.wikipedia.org/wiki/List_of_universities_in_British_Columbia",
        "source_url": "https://en.wikipedia.org/wiki/List_of_universities_in_British_Columbia",
    }
    fixed, reasons = scrub_website_policy(row)
    assert "website" not in fixed or not fixed.get("website")
    assert any("source_url" in r for r in reasons)
    assert fixed["name"] == "University of British Columbia"


def test_scrub_list_path_website_kept_name():
    row = {
        "name": "Langara College",
        "website": "https://en.wikipedia.org/wiki/List_of_colleges_in_BC",
        "city": "Vancouver",
    }
    fixed, reasons = scrub_website_policy(
        row, source_url="https://www2.gov.bc.ca/gov/content/education/directory"
    )
    assert not fixed.get("website")
    assert fixed["city"] == "Vancouver"
    assert reasons


def test_scrub_keeps_real_institutional_website():
    row = {
        "name": "University of British Columbia",
        "website": "https://www.ubc.ca",
        "source_url": "https://en.wikipedia.org/wiki/List_of_universities_in_British_Columbia",
    }
    fixed, reasons = scrub_website_policy(row)
    assert fixed["website"] == "https://www.ubc.ca"
    assert reasons == []


def test_near_dup_merge_same_name_variant():
    rows = [
        {"name": "University of British Columbia", "city": "Vancouver"},
        {"name": "University of British Columbia", "website": "https://ubc.ca"},
        {"name": "Simon Fraser University", "city": "Burnaby"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "city", "website"]
    )
    assert merged == 1
    names = {r["name"] for r in kept}
    assert names == {
        "University of British Columbia",
        "Simon Fraser University",
    }
    ubc = next(r for r in kept if "British Columbia" in r["name"])
    # Richest row wins and gaps fill
    assert ubc.get("website") == "https://ubc.ca"
    assert ubc.get("city") == "Vancouver"


def test_near_dup_merge_by_website_host():
    rows = [
        {"name": "UBC", "website": "https://www.ubc.ca/about"},
        {"name": "Univ of BC", "website": "https://ubc.ca"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "website"]
    )
    assert merged == 1
    assert len(kept) == 1


def test_near_dup_does_not_collapse_shared_wikipedia_host():
    """Multi-tenant hosts must NOT identity-merge unrelated entities."""
    rows = [
        {"name": "Ashton College", "website": "https://en.wikipedia.org/wiki/Ashton_College"},
        {"name": "Langara College", "website": "https://en.wikipedia.org/wiki/Langara_College"},
        {"name": "BCIT", "website": "https://en.wikipedia.org/wiki/BCIT"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "website"]
    )
    assert merged == 0
    assert len(kept) == 3


def test_near_dup_keeps_college_vs_university_distinct():
    """Edu type words are identity-bearing — must not over-normalize."""
    rows = [
        {"name": "St. Mary's College", "city": "A"},
        {"name": "St. Mary's University", "city": "B"},
        {"name": "Columbia College", "city": "C"},
        {"name": "Columbia University", "city": "D"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "city"]
    )
    assert merged == 0
    assert len(kept) == 4


def test_near_dup_merges_legal_suffix_only():
    rows = [
        {"name": "Acme Inc", "city": "SF"},
        {"name": "Acme Corp", "website": "https://acme.example"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "city", "website"]
    )
    assert merged == 1
    assert len(kept) == 1
    assert kept[0].get("website") == "https://acme.example"
    assert kept[0].get("city") == "SF"


def test_listings_path_is_not_list_page():
    """Substring /list must not scrub real /listings homepages."""
    row = {
        "name": "Listings Co",
        "website": "https://listings.example.com/homes",
        "source_url": "https://directory.example.com/directory",
    }
    fixed, reasons = scrub_website_policy(row)
    assert fixed.get("website") == "https://listings.example.com/homes"
    assert reasons == []


def test_quality_gate_end_to_end():
    list_url = "https://en.wikipedia.org/wiki/List_of_universities_in_British_Columbia"
    rows = [
        {
            "name": "University of British Columbia",
            "website": list_url,
            "city": "Vancouver",
            "source_url": list_url,
        },
        {
            "name": "University of British Columbia",
            "website": "https://www.ubc.ca",
            "source_url": list_url,
        },
        {
            "name": "About",  # would be dropped by A1; gate still handles
            "website": list_url,
            "source_url": list_url,
        },
        {
            "name": "Simon Fraser University",
            "website": "https://www.sfu.ca",
            "source_url": list_url,
        },
    ]
    v = apply_discovery_quality_gate(
        rows, "name", ["name", "city", "website"]
    )
    assert v.websites_scrubbed >= 1
    assert v.near_dups_merged >= 1
    names = {r["name"] for r in v.rows}
    assert "University of British Columbia" in names
    assert "Simon Fraser University" in names
    ubc = next(r for r in v.rows if r["name"] == "University of British Columbia")
    assert ubc.get("website") == "https://www.ubc.ca"


def test_page_yield_score_directory_high():
    text = (
        "British Columbia post-secondary institutions\n"
        "name | city\n"
        "University of British Columbia | Vancouver\n"
        "Simon Fraser University | Burnaby\n"
        "University of Victoria | Victoria\n"
        "BCIT | Burnaby\n"
        "Langara College | Vancouver\n"
        "Kwantlen Polytechnic University | Surrey\n"
    )
    score = page_yield_score(
        text, query="universities and colleges in British Columbia"
    )
    assert score >= 0.35
    assert page_looks_like_list(
        text, query="universities and colleges in British Columbia"
    )


def test_page_yield_score_nav_shell_low():
    text = "Home\nMenu\nAbout\nContact\nLogin\nSearch\n"
    score = page_yield_score(text, query="universities in BC")
    assert score < 0.22
    assert not page_looks_like_list(text, query="universities in BC")


def test_normalize_and_host_helpers():
    # Leading "the" stripped; "university" KEPT (identity-bearing).
    key = normalize_entity_key("The University of British Columbia")
    assert key.startswith("university")
    assert "british" in key
    assert registrable_host("https://www.ubc.ca/foo") == "ubc.ca"
    assert registrable_host("ubc.ca") == "ubc.ca"
