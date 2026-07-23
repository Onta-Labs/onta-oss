"""ONTA-393: A1 cell/row validators — reject nav chrome and type-invalid cells
before scraped rows become graph entities.

The dogfood (tenant ``july23`` / KG ``first-graph``, job ``7c6edadd…``) wrote
entities named ``About`` / ``About Us``, cities that were years (``1971``,
``1969 (national)``), enrolment phrases as addresses (``14,000 learners
annually``), and multi-campus free text as websites — all because a non-empty cell
was accepted verbatim (fill rate ≠ correctness). These tests are the acceptance bar
for the deterministic validators that drop that garbage at the A1 boundary:

  * the nav-chrome NAME blocklist (a bad key cell drops the whole row),
  * the city-as-year rule,
  * the enrolment-phrase-as-address rule,
  * the website URL-shape rule,
  * attribute→role classification (so ``capacity`` is not a city and an
    ``email_address`` is not a street address),
  * and the row-level :func:`screen_row` verdict wiring the above together.
"""
from __future__ import annotations

import pytest

from cograph_client.pipeline.a1_validators import (
    ROLE_ADDRESS,
    ROLE_CITY,
    ROLE_NAME,
    ROLE_WEBSITE,
    classify_attribute,
    screen_row,
    validate_address,
    validate_cell,
    validate_city,
    validate_name,
    validate_website,
)


# --------------------------------------------------------------------------- #
# NAME — the nav-chrome blocklist (drop the whole row)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "chrome",
    [
        "About",
        "About Us",
        "about us",          # case-insensitive
        "ABOUT US",
        "  About Us  ",       # whitespace-normalized
        "About  Us",          # internal-whitespace-normalized
        "Contact",
        "Contact Us",
        "Home",
        "Menu",
        "References",
        "See also",
        "Skip to content",    # prefix chrome
        "Skip to main content",
        "List of universities in British Columbia",  # prefix chrome
        "Jump to navigation",
    ],
)
def test_nav_chrome_names_are_rejected(chrome):
    reason = validate_name(chrome)
    assert reason is not None
    assert "nav-chrome" in reason


@pytest.mark.parametrize(
    "name",
    [
        "Langara College",
        "University of British Columbia",
        "Camosun College",
        "Vancouver Community College",
        "École Polytechnique",         # accented letters count as alnum
        "3M",                            # a real short alnum name
    ],
)
def test_real_institution_names_pass(name):
    assert validate_name(name) is None


def test_name_too_short_is_rejected():
    assert validate_name("A") is not None
    assert validate_name("") is not None
    assert validate_name("  ") is not None


def test_name_pure_punctuation_is_rejected():
    assert validate_name("—") is not None
    assert validate_name("...") is not None
    assert validate_name("|") is not None


def test_name_none_is_rejected():
    # A missing key cell is "too short", never a valid entity name.
    assert validate_name(None) is not None


# --------------------------------------------------------------------------- #
# CITY — year-like / URL / enrolment (scrub the cell)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_city",
    [
        "1971",
        "1969 (national)",
        "1975",
        "2003",
    ],
)
def test_city_year_like_is_rejected(bad_city):
    reason = validate_city(bad_city)
    assert reason is not None
    assert "year" in reason


@pytest.mark.parametrize(
    "good_city",
    [
        "Vancouver",
        "Prince George",
        "St. John's",
        "Victoria",
        "Nanaimo",
        "Hundred",           # a real WV town — a bare magnitude word must NOT trip
    ],
)
def test_real_cities_pass(good_city):
    assert validate_city(good_city) is None


def test_city_url_like_is_rejected():
    assert validate_city("https://langara.ca") is not None
    assert validate_city("www.ubc.ca") is not None


def test_city_enrolment_phrase_is_rejected():
    assert validate_city("14,000 learners annually") is not None
    assert validate_city("Thousands annually") is not None


def test_city_empty_is_not_invalid():
    # An empty cell is "unfilled", not "invalid" — nothing to reject.
    assert validate_city("") is None
    assert validate_city(None) is None


# --------------------------------------------------------------------------- #
# ADDRESS — enrolment/headcount phrase WITHOUT a street cue (scrub the cell)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_addr",
    [
        "14,000 learners annually",
        "Thousands annually",
        "Approximately 5,000 students",
        "12,500 full-time equivalent",
        "3000 pupils per year",
    ],
)
def test_address_enrolment_phrase_is_rejected(bad_addr):
    reason = validate_address(bad_addr)
    assert reason is not None
    assert "enrolment phrase" in reason


@pytest.mark.parametrize(
    "good_addr",
    [
        "1234 Main Street, Vancouver, BC",
        "100 West 49th Avenue",
        "555 Great Northern Way",
        "PO Box 3010, Victoria BC V8W 3N7",
        # A real address that ALSO mentions students keeps its street cue.
        "900 McGill Road, home to 5,000 students",
        "Suite 400, 1055 West Georgia St",
    ],
)
def test_real_addresses_pass(good_addr):
    assert validate_address(good_addr) is None


def test_address_empty_is_not_invalid():
    assert validate_address("") is None
    assert validate_address(None) is None


# --------------------------------------------------------------------------- #
# WEBSITE — require a URL/host shape (scrub the cell)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "good_site",
    [
        "https://www.langara.ca",
        "http://ubc.ca/admissions",
        "www.camosun.ca",
        "ubc.ca",
        "example.co.uk",
        "See our site at vcc.ca for details",   # a host anywhere in the text
    ],
)
def test_websites_with_url_shape_pass(good_site):
    assert validate_website(good_site) is None


@pytest.mark.parametrize(
    "bad_site",
    [
        "Multi-campus free text without URL shape",
        "Main and downtown campuses",
        "Contact admissions",
        "@LangaraCollege",
    ],
)
def test_websites_without_url_shape_are_rejected(bad_site):
    reason = validate_website(bad_site)
    assert reason is not None
    assert "URL/host shape" in reason


def test_website_empty_is_not_invalid():
    assert validate_website("") is None
    assert validate_website(None) is None


# --------------------------------------------------------------------------- #
# Attribute → role classification
# --------------------------------------------------------------------------- #
def test_key_attribute_is_the_name_role():
    assert classify_attribute("name", key_attr="name") == ROLE_NAME
    assert classify_attribute("institution", key_attr="institution") == ROLE_NAME


def test_role_classification_by_leaf_tokens():
    assert classify_attribute("city", key_attr="name") == ROLE_CITY
    assert classify_attribute("home_city", key_attr="name") == ROLE_CITY
    assert classify_attribute("website", key_attr="name") == ROLE_WEBSITE
    assert classify_attribute("homepage_url", key_attr="name") == ROLE_WEBSITE
    assert classify_attribute("address", key_attr="name") == ROLE_ADDRESS
    assert classify_attribute("street_address", key_attr="name") == ROLE_ADDRESS


def test_capacity_is_not_a_city():
    # Token match, not substring — "capacity" contains "city" as a substring but is
    # a distinct token, so it must NOT be validated as a city.
    assert classify_attribute("capacity", key_attr="name") is None


def test_email_address_is_not_a_street_address():
    # An email attribute must keep its value — the enrolment/street rules do not
    # apply to it.
    assert classify_attribute("email_address", key_attr="name") is None
    assert classify_attribute("contact_email", key_attr="name") is None


def test_unknown_attribute_has_no_validator():
    assert classify_attribute("phone", key_attr="name") is None
    assert classify_attribute("rating", key_attr="name") is None
    assert validate_cell("phone", "anything") is None  # unknown role → no check


def test_validate_cell_dispatches_by_role():
    assert validate_cell(ROLE_CITY, "1971") is not None
    assert validate_cell(ROLE_CITY, "Vancouver") is None
    assert validate_cell(ROLE_NAME, "About") is not None


# --------------------------------------------------------------------------- #
# screen_row — the row-level verdict
# --------------------------------------------------------------------------- #
ATTRS = ["name", "city", "website", "address"]


def test_screen_row_drops_whole_row_on_chrome_name():
    verdict = screen_row(
        {"name": "About Us", "city": "Vancouver", "website": "https://x.ca"},
        key_attr="name",
        attributes=ATTRS,
    )
    assert verdict.drop_row is True
    assert "nav-chrome" in verdict.row_reason
    # A dropped row reports no per-cell scrubs (the whole row is gone).
    assert verdict.scrubbed == {}


def test_screen_row_scrubs_only_invalid_cells_keeps_real_row():
    verdict = screen_row(
        {
            "name": "Camosun College",
            "city": "1971",                       # year → scrub
            "website": "camosun.ca",              # valid → keep
            "address": "3,000 students annually",  # enrolment → scrub
        },
        key_attr="name",
        attributes=ATTRS,
    )
    assert verdict.drop_row is False
    assert set(verdict.scrubbed) == {"city", "address"}
    assert "year" in verdict.scrubbed["city"]
    assert "enrolment phrase" in verdict.scrubbed["address"]


def test_screen_row_clean_row_has_no_verdict():
    verdict = screen_row(
        {
            "name": "Langara College",
            "city": "Vancouver",
            "website": "https://langara.ca",
            "address": "100 West 49th Avenue, Vancouver, BC",
        },
        key_attr="name",
        attributes=ATTRS,
    )
    assert verdict.drop_row is False
    assert verdict.scrubbed == {}


def test_screen_row_does_not_mutate_input():
    row = {"name": "Camosun College", "city": "1971"}
    screen_row(row, key_attr="name", attributes=["name", "city"])
    # The pure verdict must not touch the caller's dict — scrubbing happens in the
    # caller against a copy.
    assert row == {"name": "Camosun College", "city": "1971"}


def test_screen_row_keeps_row_when_key_cell_absent():
    # Registry/API and LLM-projection rows may not carry the key verbatim — the name
    # is minted downstream. An absent/empty key is NOT chrome, so the row survives
    # (and its non-key cells are still validated).
    for row in (
        {"npi": "1234567893", "last_name": "GARCIA"},   # no "name" key at all
        {"name": "", "last_name": "GARCIA"},              # present-but-empty key
        {"name": "   ", "last_name": "GARCIA"},           # whitespace-only key
    ):
        verdict = screen_row(row, key_attr="name", attributes=["name", "last_name"])
        assert verdict.drop_row is False, row


def test_screen_row_ignores_unknown_and_internal_keys():
    # A cell whose attribute is not in the confirmed set (or classifies to no role)
    # is left alone.
    verdict = screen_row(
        {"name": "Langara College", "source_url": "not a url", "phone": "x"},
        key_attr="name",
        attributes=["name"],   # only the key is a confirmed attribute here
    )
    assert verdict.drop_row is False
    assert verdict.scrubbed == {}
