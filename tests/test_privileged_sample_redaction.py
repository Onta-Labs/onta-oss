"""Privileged CSV schema-inference sample redaction.

Columns named like ssn / privileged* / secret* / password / api_key must not
leak cell values into LLM prompts. Keys stay so the model can still map them.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from cograph_client.resolver.sensitivity import (
    REDACTED,
    is_privileged_column_name,
    redact_privileged_profile_examples,
    redact_privileged_sample_rows,
)


class TestIsPrivilegedColumnName:
    @pytest.mark.parametrize(
        "name",
        [
            "ssn",
            "SSN",
            "customer_ssn",
            "social_security_number",
            "password",
            "user_password",
            "passwd",
            "secret",
            "secret_token",
            "privileged",
            "privileged_note",
            "PrivilegedFlag",
            "api_key",
            "API-KEY",
            "apikey",
            "access_token",
            "private_key",
            "auth_token",
            "credentials",
            "credit_card",
            "cvv",
        ],
    )
    def test_privileged_names(self, name):
        assert is_privileged_column_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "name",
            "email",
            "city",
            "status",
            "description",
            "id",
            "created_at",
            "amount",
            "title",
            # near-misses that must NOT redact ordinary columns
            "session_id",
            "assessment",
            "access_level",
            "key_features",
        ],
    )
    def test_public_names(self, name):
        assert is_privileged_column_name(name) is False


class TestRedactSampleRows:
    def test_redacts_values_keeps_keys(self):
        rows = [
            {"name": "Ada", "ssn": "123-45-6789", "city": "SF"},
            {"name": "Bob", "ssn": "987-65-4321", "city": "NY"},
        ]
        out = redact_privileged_sample_rows(rows)
        assert out[0]["name"] == "Ada"
        assert out[0]["city"] == "SF"
        assert out[0]["ssn"] == REDACTED
        assert out[1]["ssn"] == REDACTED
        # input untouched
        assert rows[0]["ssn"] == "123-45-6789"

    def test_multiple_privileged_columns(self):
        rows = [{"secret_token": "tok", "password": "hunter2", "title": "x"}]
        out = redact_privileged_sample_rows(rows)
        assert out[0] == {
            "secret_token": REDACTED,
            "password": REDACTED,
            "title": "x",
        }

    def test_empty_and_noop(self):
        assert redact_privileged_sample_rows([]) == []
        rows = [{"name": "Ada"}]
        assert redact_privileged_sample_rows(rows) == [{"name": "Ada"}]


class TestRedactProfileExamples:
    def test_scrubs_examples_only(self):
        from cograph_client.resolver.models import ColumnProfile, TableProfile, ValueShape

        profile = TableProfile(
            rows_profiled=2,
            total_rows=2,
            columns=[
                ColumnProfile(
                    name="ssn",
                    completeness=1.0,
                    distinct=2,
                    uniqueness=1.0,
                    card_ratio=1.0,
                    value_shape=ValueShape.CODE_ID,
                    examples=["123-45-6789", "987-65-4321"],
                ),
                ColumnProfile(
                    name="city",
                    completeness=1.0,
                    distinct=2,
                    uniqueness=1.0,
                    card_ratio=1.0,
                    value_shape=ValueShape.LABEL,
                    examples=["SF", "NY"],
                ),
            ],
        )
        redact_privileged_profile_examples(profile)
        assert profile.column("ssn").examples == [REDACTED, REDACTED]
        assert profile.column("city").examples == ["SF", "NY"]
        # stats preserved
        assert profile.column("ssn").distinct == 2


class TestCsvResolverWiresRedaction:
    """Legacy + v2 paths must embed redacted values in the LLM user prompt."""

    @pytest.mark.asyncio
    async def test_legacy_prompt_redacts_ssn(self, monkeypatch):
        monkeypatch.setenv("OMNIX_CSV_INFERENCE_V2", "0")

        from cograph_client.resolver.csv_resolver import CSVResolver

        captured: dict = {}

        async def fake_call(self, user_content, temperature=0.0):
            captured["user"] = user_content
            # Minimal valid legacy mapping JSON.
            return {
                "entity_type": "Person",
                "columns": [
                    {"column_name": "name", "role": "type_id", "attribute_name": "name", "datatype": "string"},
                    {"column_name": "ssn", "role": "attribute", "attribute_name": "ssn", "datatype": "string"},
                    {"column_name": "city", "role": "attribute", "attribute_name": "city", "datatype": "string"},
                ],
            }

        monkeypatch.setattr(CSVResolver, "_call_llm", fake_call)

        resolver = CSVResolver(client=MagicMock(), openrouter_key="sk-test")
        rows = [
            {"name": "Ada", "ssn": "123-45-6789", "city": "SF"},
            {"name": "Bob", "ssn": "987-65-4321", "city": "NY"},
        ]
        await resolver.infer_schema(
            headers=["name", "ssn", "city"],
            sample_rows=rows,
            existing_types={},
            total_rows=2,
        )
        user = captured["user"]
        assert "123-45-6789" not in user
        assert "987-65-4321" not in user
        assert REDACTED in user
        # non-privileged values still present
        assert "Ada" in user or "SF" in user

    @pytest.mark.asyncio
    async def test_v2_prompt_redacts_privileged(self, monkeypatch):
        monkeypatch.setenv("OMNIX_CSV_INFERENCE_V2", "1")

        from cograph_client.resolver.csv_resolver import CSVResolver

        captured_users: list[str] = []

        # Capture every v2 pass user prompt; raise after first capture so we
        # don't need a full valid multi-pass schema — redaction happens before
        # the first LLM call.
        async def fake_v2(self, system, user_content, temperature=0.0, max_tokens=4096):
            captured_users.append(user_content)
            raise RuntimeError("stop-after-capture")

        monkeypatch.setattr(CSVResolver, "_call_llm_v2", fake_v2)

        resolver = CSVResolver(client=MagicMock(), openrouter_key="sk-test")
        secret = "TOP-SECRET-VALUE-XYZ"
        rows = [
            {"name": "Ada", "privileged_code": secret},
            {"name": "Bob", "privileged_code": secret + "2"},
        ]
        with pytest.raises(RuntimeError, match="stop-after-capture"):
            await resolver.infer_schema(
                headers=["name", "privileged_code"],
                sample_rows=rows,
                existing_types={},
                total_rows=2,
            )

        assert captured_users, "expected at least one LLM user prompt"
        blob = "\n".join(captured_users)
        assert secret not in blob
        assert secret + "2" not in blob
        assert REDACTED in blob
        # profile examples path also redacted (same secret would appear as example)
        assert "Ada" in blob  # non-privileged value still present
