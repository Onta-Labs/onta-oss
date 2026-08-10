"""Discovery quality gate — website policy + near-dup merge + page yield probe."""

from __future__ import annotations

from infona_client.pipeline.discovery_quality import (
    alnum_identity,
    apply_discovery_quality_gate,
    catalog_identity_key,
    catalog_path_segments,
    catalog_surface_keys,
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


def test_source_url_is_not_website_identity():
    """Rows sharing a list-page source_url must NOT near-dup-merge via host."""
    rows = [
        {
            "name": "anthropic/claude-opus-4-8",
            "context_length": "200000",
            "source_url": "https://src.example/page-0",
        },
        {
            "name": "openai/gpt-5",
            "context_length": "400000",
            "source_url": "https://src.example/page-0",
        },
        {
            "name": "google/gemini-2.5-flash",
            "context_length": "1000000",
            "source_url": "https://src.example/page-0",
        },
        {
            "name": "meta/llama-4",
            "context_length": "128000",
            "source_url": "https://src.example/page-0",
        },
    ]
    v = apply_discovery_quality_gate(
        rows, "name", ["name", "context_length"]
    )
    assert v.near_dups_merged == 0
    assert len(v.rows) == 4


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


# --------------------------------------------------------------------------- #
# R1 — structural catalog-path identity (multi-domain; no brand logic in prod)
# --------------------------------------------------------------------------- #


def test_catalog_path_helpers_structural_only():
    assert catalog_path_segments("org/model-slug") == ("org", "model-slug")
    assert catalog_path_segments("@scope/pkg") == ("@scope", "pkg")
    assert catalog_path_segments("owner/name") == ("owner", "name")
    assert catalog_path_segments("Widget Pro") is None
    assert catalog_path_segments("https://example.com/a/b") is None
    assert catalog_path_segments("solo") is None
    # @scope stripped in identity key so scoped and unscoped package ids match.
    assert catalog_identity_key(("@scope", "pkg")) == catalog_identity_key(
        ("scope", "pkg")
    )
    assert catalog_identity_key(("a", "b-hd")) != catalog_identity_key(
        ("a", "b-turbo")
    )
    surfaces = catalog_surface_keys(("acme", "widget-pro"))
    assert "widgetpro" in surfaces
    assert "acmewidgetpro" in surfaces
    assert alnum_identity("Widget Pro") == "widgetpro"


def test_structural_identity_merges_model_catalog_path_with_title():
    """Domain: model catalogs — org/slug ↔ display title."""
    rows = [
        {
            "name": "acme-labs/widget-pro",
            "context_length": "8192",
            "provider": "acme-labs",
        },
        {
            "name": "Widget Pro",
            "description": "flagship widget model",
            "provider": "acme-labs",
        },
        {"name": "other-org/unrelated-tool", "context_length": "4096"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "context_length", "description", "provider"]
    )
    assert merged == 1
    assert len(kept) == 2
    survivor = next(r for r in kept if "widget" in r["name"].casefold())
    # Prefer catalog-path form as survivor.
    assert survivor["name"] == "acme-labs/widget-pro"
    assert survivor.get("context_length") == "8192"
    assert survivor.get("description") == "flagship widget model"
    assert any(r["name"] == "other-org/unrelated-tool" for r in kept)


def test_structural_identity_merges_package_scope_with_title():
    """Domain: software packages — @scope/pkg or scope/pkg ↔ display title."""
    rows = [
        {"name": "Http Client", "license": "MIT", "stars": "1200"},
        {"name": "@tools/http-client", "version": "3.2.1"},
        {"name": "tools/http-client", "downloads": "9m"},  # same path, no @
        {"name": "@tools/http-server", "version": "1.0.0"},  # sibling — keep
    ]
    kept, merged, _ = merge_near_duplicates(
        rows,
        "name",
        plan_attrs=["name", "license", "stars", "version", "downloads"],
    )
    assert merged >= 2  # free-text + two catalog forms of same package
    names = {r["name"] for r in kept}
    assert "@tools/http-server" in names
    # One survivor for the client package; catalog form preferred.
    client = [
        r
        for r in kept
        if "http-client" in r["name"] or r["name"] == "Http Client"
    ]
    assert len(client) == 1
    assert "/" in client[0]["name"]
    assert "http-client" in client[0]["name"]
    assert client[0].get("license") == "MIT"
    assert client[0].get("version") == "3.2.1"
    assert client[0].get("downloads") == "9m"


def test_structural_identity_merges_dataset_owner_name_with_title():
    """Domain: datasets — owner/name ↔ title."""
    rows = [
        {
            "name": "civic-data/census-2020",
            "rows": "331m",
            "source_url": "https://catalog.example/datasets/1",
        },
        {
            "name": "Census 2020",
            "format": "parquet",
            "source_url": "https://catalog.example/datasets/1",
        },
        {
            "name": "civic-data/census-2010",
            "rows": "309m",
        },
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "rows", "format"]
    )
    assert merged == 1
    assert len(kept) == 2
    census_2020 = next(r for r in kept if "2020" in r["name"])
    assert census_2020["name"] == "civic-data/census-2020"
    assert census_2020.get("format") == "parquet"
    assert census_2020.get("rows") == "331m"
    assert any(r["name"] == "civic-data/census-2010" for r in kept)


def test_structural_identity_does_not_merge_sibling_catalog_suffixes():
    """Distinct catalog paths must never merge (hd vs turbo, etc.)."""
    rows = [
        {"name": "acme/codec-hd", "bitrate": "320"},
        {"name": "acme/codec-turbo", "bitrate": "128"},
        {"name": "Codec HD", "language": "en"},  # joins hd only
        {"name": "vendor/codec-hd", "bitrate": "256"},  # different org, same tail
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "bitrate", "language"]
    )
    # Free-text joins at most one catalog path; sibling paths stay distinct.
    catalog_names = sorted(r["name"] for r in kept if "/" in r["name"])
    assert "acme/codec-hd" in catalog_names
    assert "acme/codec-turbo" in catalog_names
    assert "vendor/codec-hd" in catalog_names
    # turbo must not absorb hd attrs or free-text incorrectly
    turbo = next(r for r in kept if r["name"] == "acme/codec-turbo")
    assert turbo.get("language") is None or turbo.get("language") == ""
    assert turbo.get("bitrate") == "128"
    # free-text "Codec HD" merges into one of the *-hd catalog rows (prefer path)
    assert not any(r["name"] == "Codec HD" for r in kept)
    hd_rows = [r for r in kept if r["name"].endswith("codec-hd")]
    assert any(r.get("language") == "en" for r in hd_rows)
    assert merged >= 1


def test_structural_identity_catalog_before_free_text_order_independent():
    """Merge works regardless of whether catalog path or title arrives first."""
    catalog_first = [
        {"name": "pubs/annual-report-2024", "pages": "48"},
        {"name": "Annual Report 2024", "year": "2024"},
    ]
    title_first = list(reversed(catalog_first))
    for rows in (catalog_first, title_first):
        kept, merged, _ = merge_near_duplicates(
            rows, "name", plan_attrs=["name", "pages", "year"]
        )
        assert merged == 1
        assert len(kept) == 1
        assert kept[0]["name"] == "pubs/annual-report-2024"
        assert kept[0].get("pages") == "48"
        assert kept[0].get("year") == "2024"


def test_structural_identity_full_path_surface_match():
    """Free-text that is a display transform of the full path also merges."""
    rows = [
        {"name": "team/alpha-bot", "status": "stable"},
        {"name": "team alpha bot", "owner": "team"},
    ]
    kept, merged, _ = merge_near_duplicates(
        rows, "name", plan_attrs=["name", "status", "owner"]
    )
    assert merged == 1
    assert len(kept) == 1
    assert kept[0]["name"] == "team/alpha-bot"
    assert kept[0].get("owner") == "team"
    assert kept[0].get("status") == "stable"
