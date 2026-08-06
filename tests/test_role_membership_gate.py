"""ONTA-460 / WS2 / R2 — structural role-membership gate.

Batch-relative role inversion: a row whose key equals another row's
provider/manufacturer/organization/… value is a role entity mistaken for an
instance under single focus_type stamping.

Hard rules under test:
  * no brand / voice / platform denylists in the production module
  * multi-domain fixtures (physicians, products, models)
  * uncertain → keep (single-token real instance without role evidence)
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from cograph_client.pipeline.role_membership_gate import (
    DEFAULT_ROLE_ATTRIBUTES,
    alnum_norm,
    identity_rank,
    is_catalog_path,
    screen_role_membership,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _names(rows, key="name"):
    return [r.get(key) for r in rows]


def _kept_names(verdict, key="name"):
    return _names(verdict.kept, key)


def _dropped_names(verdict, key="name"):
    return _names(verdict.dropped, key)


# --------------------------------------------------------------------------- #
# Unit primitives
# --------------------------------------------------------------------------- #

def test_alnum_norm_collapses_case_and_punct():
    assert alnum_norm("UCSF Medical Center") == "ucsfmedicalcenter"
    assert alnum_norm("  Acme-Corp  ") == "acmecorp"
    assert alnum_norm("openai/gpt-4") == "openaigpt4"
    assert alnum_norm("") == ""
    assert alnum_norm(None) == ""


def test_is_catalog_path_structural():
    assert is_catalog_path("org/slug") is True
    assert is_catalog_path("scope/pkg/extra") is True
    assert is_catalog_path("@scope/pkg") is True
    assert is_catalog_path("OpenAI") is False
    assert is_catalog_path("just-a-name") is False
    assert is_catalog_path("/leading") is False
    assert is_catalog_path("trailing/") is False
    assert is_catalog_path("") is False
    # Shared with R1 discovery_quality.catalog_path_segments — not bare slashes.
    assert is_catalog_path("https://host/a/b") is False
    assert is_catalog_path("//cdn.example/x/y") is False
    assert is_catalog_path("2024/2025 Budget") is False  # whitespace segment


def test_identity_rank_catalog_beats_freetext():
    assert identity_rank("org/slug") > identity_rank("OpenAI")
    assert identity_rank("OpenAI") > identity_rank("")
    assert identity_rank(None) == 0


def test_default_role_attributes_are_schema_slots_not_entities():
    # Vocabulary is role *attribute names*, never brand tokens.
    for leaf in DEFAULT_ROLE_ATTRIBUTES:
        assert re.fullmatch(r"[a-z_]+", leaf), leaf
    assert "provider" in DEFAULT_ROLE_ATTRIBUTES
    assert "manufacturer" in DEFAULT_ROLE_ATTRIBUTES
    assert "organization" in DEFAULT_ROLE_ATTRIBUTES
    # Hierarchical dual-use leaves are NOT defaults (false-drop review ONTA-465).
    assert "parent" not in DEFAULT_ROLE_ATTRIBUTES
    assert "source" not in DEFAULT_ROLE_ATTRIBUTES
    assert "owner" not in DEFAULT_ROLE_ATTRIBUTES


def test_url_key_is_not_catalog_rank_two():
    """URL-shaped keys must not outrank free-text (aligned with R1)."""
    assert identity_rank("https://docs.example.com/models/foo") == 1
    assert identity_rank("acme/foo") == 2


def test_parent_hierarchy_not_dropped_under_defaults():
    """Same-type intermediate nodes listed as parent must keep under defaults."""
    rows = [
        {"name": "iPhone 15 Pro", "parent": "iPhone 15", "sku": "A1"},
        {"name": "iPhone 15", "parent": "iPhone", "sku": "A0", "year": "2023"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "iPhone 15" in _kept_names(v)
    assert "iPhone 15 Pro" in _kept_names(v)
    assert _dropped_names(v) == []


def test_parent_still_droppable_when_caller_opts_in():
    """Callers may still pass parent via role_attributes for domain-specific runs."""
    rows = [
        {"name": "Parent Org"},
        {"name": "Child Org", "parent": "Parent Org", "city": "SF"},
    ]
    v = screen_role_membership(
        rows, key_attr="name", role_attributes=frozenset({"parent"})
    )
    assert "Parent Org" in _dropped_names(v)
    assert "Child Org" in _kept_names(v)


def test_sparse_brand_drops_when_catalog_path_inventory_present():
    """Incident class: bare brand (name==provider) next to org/slug models drops."""
    rows = [
        {"name": "fish-audio/s2.1-pro", "provider": "fish-audio"},
        {"name": "S2.1 Pro", "provider": "fish-audio"},
        {"name": "ElevenLabs", "provider": "ElevenLabs"},
        {"name": "Azure", "provider": "Azure"},
        {"name": "hexgrad/kokoro-82m", "provider": "hexgrad"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "ElevenLabs" in _dropped_names(v)
    assert "Azure" in _dropped_names(v)
    assert "fish-audio/s2.1-pro" in _kept_names(v)
    assert "hexgrad/kokoro-82m" in _kept_names(v)


def test_sparse_self_provider_kept_without_catalog_path_batch():
    """Pure company list (no org/slug ids) must not drop name==provider rows."""
    rows = [
        {"name": "Acme", "provider": "Acme"},
        {"name": "Contoso", "provider": "Contoso"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert set(_kept_names(v)) == {"Acme", "Contoso"}
    assert _dropped_names(v) == []

# --------------------------------------------------------------------------- #
# Domain 1 — Physicians: hospital name stamped as Physician
# --------------------------------------------------------------------------- #

def test_physicians_hospital_name_dropped_when_used_as_provider():
    rows = [
        {"name": "UCSF Medical Center"},  # role entity (hospital) as fake Physician
        {
            "name": "Dr. Jane Smith",
            "provider": "UCSF Medical Center",
            "specialty": "Cardiology",
        },
        {
            "name": "Dr. Alex Chen",
            "provider": "UCSF Medical Center",
            "specialty": "Cardiology",
        },
        {
            "name": "Dr. Pat Lee",
            "provider": "Stanford Health",
            "specialty": "Oncology",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "UCSF Medical Center" in _dropped_names(v)
    assert "Dr. Jane Smith" in _kept_names(v)
    assert "Dr. Alex Chen" in _kept_names(v)
    assert "Dr. Pat Lee" in _kept_names(v)
    # Stanford Health is NOT in the batch as a bare row → nothing to drop for it
    assert "Stanford Health" not in _dropped_names(v)
    assert any("role-inversion" in r for r in v.reasons)


def test_physicians_organization_attr_also_triggers():
    rows = [
        {"name": "County General Hospital", "city": "Oakland"},
        {
            "name": "Maria Gomez, MD",
            "organization": "County General Hospital",
            "npi": "1234567890",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert _dropped_names(v) == ["County General Hospital"]
    assert _kept_names(v) == ["Maria Gomez, MD"]


# --------------------------------------------------------------------------- #
# Domain 2 — Products: manufacturer name stamped as Product
# --------------------------------------------------------------------------- #

def test_products_manufacturer_row_dropped():
    rows = [
        {"name": "Acme Corp"},  # manufacturer mistaken for product
        {
            "name": "Super Widget",
            "manufacturer": "Acme Corp",
            "sku": "SW-1",
            "price": "19.99",
        },
        {
            "name": "Mega Gadget",
            "manufacturer": "Acme Corp",
            "sku": "MG-2",
        },
        {
            "name": "Other Tool",
            "manufacturer": "Beta Industries",
            "sku": "OT-3",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "Acme Corp" in _dropped_names(v)
    assert set(_kept_names(v)) == {"Super Widget", "Mega Gadget", "Other Tool"}
    assert "Beta Industries" not in _dropped_names(v)


def test_products_vendor_attr_triggers():
    rows = [
        {"name": "Northwind Traders"},
        {"name": "Cereal Box", "vendor": "Northwind Traders", "upc": "0001"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert _dropped_names(v) == ["Northwind Traders"]
    assert _kept_names(v) == ["Cereal Box"]


# --------------------------------------------------------------------------- #
# Domain 3 — Models: provider string stamped as Model
# --------------------------------------------------------------------------- #

def test_models_provider_string_row_dropped():
    rows = [
        {"name": "OpenAI"},  # provider brand as fake Model
        {
            "name": "openai/gpt-4",
            "provider": "OpenAI",
            "context_length": "128000",
        },
        {
            "name": "openai/gpt-4o",
            "provider": "OpenAI",
            "context_length": "128000",
        },
        {
            "name": "anthropic/claude-3-5",
            "provider": "Anthropic",
            "context_length": "200000",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "OpenAI" in _dropped_names(v)
    assert "openai/gpt-4" in _kept_names(v)
    assert "openai/gpt-4o" in _kept_names(v)
    assert "anthropic/claude-3-5" in _kept_names(v)
    # Anthropic never appears as a bare row — not dropped
    assert "Anthropic" not in _dropped_names(v)


def test_models_sparse_self_role_with_catalog_instances():
    """name == provider on a sparse row + catalog-path instances using that provider."""
    rows = [
        {"name": "AcmeAI", "provider": "AcmeAI"},  # sparse self-role
        {
            "name": "acme/turbo-1",
            "provider": "AcmeAI",
            "context_length": "8192",
        },
        {
            "name": "acme/turbo-2",
            "provider": "AcmeAI",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "AcmeAI" in _dropped_names(v)
    assert set(_kept_names(v)) == {"acme/turbo-1", "acme/turbo-2"}


def test_models_catalog_path_not_dropped_when_referenced_as_role():
    """A real catalog-path instance must not be dropped just because another row
    lists it in a role field (equal-or-weaker free-text owner still loses to it).
    """
    rows = [
        {
            "name": "meta/llama-3",
            "provider": "Meta",
            "context_length": "8192",
        },
        {
            # free-text row that (oddly) puts a catalog path in manufacturer —
            # the catalog-path model is the stronger identity and must stay.
            "name": "Some Bundle",
            "manufacturer": "meta/llama-3",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "meta/llama-3" in _kept_names(v)
    assert "Some Bundle" in _kept_names(v)


# --------------------------------------------------------------------------- #
# Negatives — must NOT drop without role evidence
# --------------------------------------------------------------------------- #

def test_negative_single_token_instance_kept_without_role_evidence():
    """Product 'Echo' is a real one-word instance; no other row uses it as a role."""
    rows = [
        {"name": "Echo", "price": "99", "category": "speaker"},
        {"name": "Dot", "price": "49", "category": "speaker"},
        {"name": "Show", "price": "129", "category": "display"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert v.dropped == []
    assert set(_kept_names(v)) == {"Echo", "Dot", "Show"}


def test_negative_shared_city_does_not_trigger_role_drop():
    """Non-role attributes (city) must not invert membership."""
    rows = [
        {"name": "Springfield", "type_hint": "clinic"},
        {"name": "Dr. House", "city": "Springfield", "specialty": "Diagnostics"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert v.dropped == []
    assert set(_kept_names(v)) == {"Springfield", "Dr. House"}


def test_negative_source_url_is_not_a_role_slot():
    """``source_url`` contains 'source' as a substring but is provenance, not a role."""
    rows = [
        {"name": "example.com", "title": "Directory"},
        {
            "name": "Real Entity",
            "source_url": "https://example.com/list",
            "city": "SF",
        },
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert v.dropped == []
    assert "example.com" in _kept_names(v)


def test_empty_batch_and_empty_key():
    assert screen_role_membership([], key_attr="name").kept == []
    rows = [{"name": "", "provider": "X"}, {"name": "Y", "provider": "X"}]
    v = screen_role_membership(rows, key_attr="name")
    # empty key cannot match role inversion
    assert all(r.get("name") != "" or r in v.kept for r in rows)


def test_case_and_punctuation_insensitive_match():
    rows = [
        {"name": "ACME Corp."},
        {"name": "Widget", "manufacturer": "acme-corp"},
    ]
    v = screen_role_membership(rows, key_attr="name")
    assert "ACME Corp." in _dropped_names(v)
    assert "Widget" in _kept_names(v)


def test_custom_role_attributes_override():
    rows = [
        {"name": "Hospital A"},
        {"name": "Dr. Z", "employer": "Hospital A"},
    ]
    # Default vocab has no "employer" → keep both
    v0 = screen_role_membership(rows, key_attr="name")
    assert v0.dropped == []
    # Custom vocab → drop
    v1 = screen_role_membership(
        rows, key_attr="name", role_attributes=frozenset({"employer"})
    )
    assert _dropped_names(v1) == ["Hospital A"]


def test_focus_type_unused_no_type_specific_behavior():
    """focus_type may be passed but must not change structural outcomes."""
    rows = [
        {"name": "Acme"},
        {"name": "Widget", "manufacturer": "Acme"},
    ]
    v_a = screen_role_membership(rows, key_attr="name", focus_type="Model")
    v_b = screen_role_membership(rows, key_attr="name", focus_type="Physician")
    v_c = screen_role_membership(rows, key_attr="name", focus_type=None)
    assert _dropped_names(v_a) == _dropped_names(v_b) == _dropped_names(v_c) == ["Acme"]


# --------------------------------------------------------------------------- #
# Prod-module denylist guard — no incident/brand literals in the gate
# --------------------------------------------------------------------------- #

# Strings that must never appear as code literals in the production module.
# (Tests and fixtures may use them as examples of the general phenomenon.)
_FORBIDDEN_PROD_LITERALS = (
    "elevenlabs",
    "openrouter",
    "vapi",
    "playht",
    "wellsaid",
    "murf",
    "resemble",
    "cartesia",
    "deepgram",
    "tts",
)


def test_prod_module_has_no_brand_or_incident_literals():
    mod_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "cograph_client"
        / "pipeline"
        / "role_membership_gate.py"
    )
    source = mod_path.read_text(encoding="utf-8")
    # Parse so we only flag string literals, not comments about the incident.
    tree = ast.parse(source)
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.append(node.value.casefold())
    joined = "\n".join(literals)
    for bad in _FORBIDDEN_PROD_LITERALS:
        assert bad not in joined, f"prod module must not contain brand literal {bad!r}"
    # Also ban focus_type-specific name-set patterns that overfit.
    assert "if focus_type" not in source
    assert "focus_type ==" not in source
