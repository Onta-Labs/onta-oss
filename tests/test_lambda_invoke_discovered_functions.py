"""Invoke-response contract: ``discovered_entities[]`` carries FUNCTION names.

The field used to be called ``skills``. In this product a "skill" is now
type-attached, human-authored markdown PROSE consumed by LM agents
(``cograph_client.skills``) — the opposite of the executable functions this
endpoint attaches. The response field was therefore renamed to ``functions``.

Because ``DiscoveredEntity.skills`` is part of the SHIPPED
``/graphs/{tenant}/functions/{name}/invoke`` response contract, the rename is
ADDITIVE: ``functions`` is the real field, ``skills`` stays as a deprecated
alias populated identically. Both assertions below are load-bearing — the
equality one is what stops the alias from silently drifting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cograph_client.api.routes import lambda_functions


def _sparql(rows: list[dict[str, str]], variables: list[str]) -> dict:
    return {
        "head": {"vars": variables},
        "results": {
            "bindings": [
                {k: {"type": "literal", "value": v} for k, v in row.items()}
                for row in rows
            ]
        },
    }


@pytest.fixture
def invoke_response(client, mock_neptune, auth_headers, monkeypatch):
    """Drive the real invoke route with a scripted graph + stubbed side effects."""

    async def fake_query(sparql: str, *args, **kwargs):
        if "lead_investor" in sparql and "investorName" in sparql:
            return _sparql(
                [
                    {
                        "investor": "https://cograph.tech/entities/Investor/Pear_VC",
                        "investorName": "Pear_VC",
                    }
                ],
                ["investor", "investorName"],
            )
        if "filing_cik" in sparql:
            return _sparql([{"cik": "0000320193"}], ["cik"])
        # The function-registry lookup.
        return _sparql(
            [
                {
                    "name": "sec-latest-filing",
                    "type": "https://cograph.tech/types/Company",
                    "endpoint": "https://example.invalid/sec",
                    "desc": "latest filing",
                }
            ],
            ["name", "type", "endpoint", "desc"],
        )

    mock_neptune.query.side_effect = fake_query

    class _Executor:
        async def invoke(self, func_ref, payload, headers=None):
            return SimpleNamespace(output={"latest_filing_type": "10-K"})

    monkeypatch.setattr(lambda_functions, "_get_executor", lambda: _Executor())

    # The write path is exercised by its own tests; stub it so this test is
    # about the response shape only.
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(lambda_functions, "delete_facts", _noop)
    monkeypatch.setattr(lambda_functions, "insert_facts", _noop)
    monkeypatch.setattr(lambda_functions, "refresh_after_write", _noop)

    resp = client.post(
        "/graphs/test-tenant/functions/sec-latest-filing/invoke",
        json={
            "entity_uri": "https://cograph.tech/entities/Company/Acme",
            "kg_name": "demo-kg",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_discovered_entity_carries_functions(invoke_response):
    discovered = invoke_response["discovered_entities"]
    assert discovered, "cascade discovery should have produced an Investor"
    entity = discovered[0]

    assert "functions" in entity, "invoke response must expose `functions`"
    # The mapped values are FUNCTION names — parenthesised call syntax.
    assert entity["functions"] == lambda_functions.FUNCTIONS_BY_TYPE["Investor"]
    assert all(name.endswith("()") for name in entity["functions"])


def test_deprecated_skills_alias_still_present_and_equal(invoke_response):
    """`skills` is deprecated but must not be dropped or allowed to drift."""
    entity = invoke_response["discovered_entities"][0]

    assert "skills" in entity, "deprecated `skills` alias must remain for clients"
    assert entity["skills"] == entity["functions"]
