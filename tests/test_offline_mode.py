"""Unit tests for OMNIX_OFFLINE / COGRAPH_OFFLINE fail-closed network guard.

Covers cograph_client/offline.py and its wiring at the main outbound
entrypoints (LLM router, embed client, Wikidata). Default is OFF — no behavior
change for normal OSS users.
"""

from __future__ import annotations

import pytest

from cograph_client.offline import (
    OfflineModeError,
    assert_online_host,
    assert_online_url,
    filter_urls_online,
    host_allowed_offline,
    offline_allow_hosts,
    offline_enabled,
)


# ---------------------------------------------------------------------------
# Flag parsing
# ---------------------------------------------------------------------------


class TestOfflineEnabled:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE", raising=False)
        monkeypatch.delenv("COGRAPH_OFFLINE", raising=False)
        assert offline_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_omnix_truthy(self, monkeypatch, val):
        monkeypatch.delenv("COGRAPH_OFFLINE", raising=False)
        monkeypatch.setenv("OMNIX_OFFLINE", val)
        assert offline_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_omnix_falsy(self, monkeypatch, val):
        monkeypatch.delenv("COGRAPH_OFFLINE", raising=False)
        monkeypatch.setenv("OMNIX_OFFLINE", val)
        assert offline_enabled() is False

    def test_cograph_alias(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE", raising=False)
        monkeypatch.setenv("COGRAPH_OFFLINE", "1")
        assert offline_enabled() is True


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_default_loopback(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE_ALLOW_HOSTS", raising=False)
        hosts = offline_allow_hosts()
        assert "localhost" in hosts
        assert "127.0.0.1" in hosts
        assert "::1" in hosts

    def test_extra_hosts_env(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE_ALLOW_HOSTS", "ollama.local, My-VLLM.Internal ")
        hosts = offline_allow_hosts()
        assert "ollama.local" in hosts
        assert "my-vllm.internal" in hosts
        assert "localhost" in hosts  # defaults still present

    def test_host_allowed_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE_ALLOW_HOSTS", raising=False)
        assert host_allowed_offline("LocalHost") is True
        assert host_allowed_offline("127.0.0.1") is True

    def test_host_blocked_when_not_listed(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE_ALLOW_HOSTS", raising=False)
        assert host_allowed_offline("openrouter.ai") is False
        assert host_allowed_offline("api.cerebras.ai") is False
        assert host_allowed_offline("www.wikidata.org") is False


# ---------------------------------------------------------------------------
# assert_online_*
# ---------------------------------------------------------------------------


class TestAssertOnline:
    def test_noop_when_offline_off(self, monkeypatch):
        monkeypatch.delenv("OMNIX_OFFLINE", raising=False)
        monkeypatch.delenv("COGRAPH_OFFLINE", raising=False)
        # Must not raise for any host when offline is off.
        assert_online_url("https://openrouter.ai/api/v1/chat/completions")
        assert_online_host("api.cerebras.ai")

    def test_blocks_openrouter(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.delenv("OMNIX_OFFLINE_ALLOW_HOSTS", raising=False)
        with pytest.raises(OfflineModeError) as ei:
            assert_online_url(
                "https://openrouter.ai/api/v1/chat/completions",
                purpose="LLM chat completion",
            )
        msg = str(ei.value)
        assert "openrouter.ai" in msg
        assert "OMNIX_OFFLINE" in msg
        assert "OMNIX_LLM_BASE_URL" in msg  # LLM purpose → local-endpoint hint

    def test_blocks_cerebras(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        with pytest.raises(OfflineModeError) as ei:
            assert_online_url(
                "https://api.cerebras.ai/v1/chat/completions",
                purpose="LLM chat completion",
            )
        assert "cerebras.ai" in str(ei.value)

    def test_blocks_wikidata(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        with pytest.raises(OfflineModeError) as ei:
            assert_online_url(
                "https://www.wikidata.org/w/api.php",
                purpose="Wikidata enrichment lookup",
            )
        assert "wikidata.org" in str(ei.value)

    def test_allows_localhost_llm(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.delenv("OMNIX_OFFLINE_ALLOW_HOSTS", raising=False)
        assert_online_url(
            "http://127.0.0.1:11434/v1/chat/completions",
            purpose="LLM chat completion",
        )
        assert_online_url(
            "http://localhost:8080/v1/embeddings",
            purpose="embedding API",
        )

    def test_allows_extra_host(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.setenv("OMNIX_OFFLINE_ALLOW_HOSTS", "ollama.local")
        assert_online_url("http://ollama.local:11434/v1/chat/completions")

    def test_filter_urls_online(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        urls = [
            "https://example.com/a",
            "http://127.0.0.1/local",
            "https://www.wikidata.org/x",
        ]
        assert filter_urls_online(urls) == ["http://127.0.0.1/local"]


# ---------------------------------------------------------------------------
# Entrypoint wiring (raises before any HTTP)
# ---------------------------------------------------------------------------


class TestEntrypointWiring:
    @pytest.mark.asyncio
    async def test_llm_router_blocks_cloud(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.delenv("OMNIX_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OMNIX_OPENROUTER_BASE_URL", raising=False)
        monkeypatch.delenv("OMNIX_LLM_PROVIDER", raising=False)

        from cograph_client.resolver import llm_router

        # Force default cloud base even if import-time env differed.
        monkeypatch.setattr(
            llm_router,
            "_openrouter_base",
            lambda: "https://openrouter.ai/api/v1",
        )

        with pytest.raises(OfflineModeError) as ei:
            await llm_router.openrouter_chat(
                "sk-test",
                system="s",
                user="u",
            )
        assert "LLM" in str(ei.value) or "openrouter" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_llm_router_allows_local_base(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.setenv("OMNIX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")

        from cograph_client.resolver import llm_router

        captured: dict = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                return _Resp()

        monkeypatch.setattr(llm_router.httpx, "AsyncClient", _Client)
        out = await llm_router.openrouter_chat("sk-test", system="s", user="u")
        assert out == "ok"
        assert "127.0.0.1" in captured["url"]

    @pytest.mark.asyncio
    async def test_embed_client_blocks_cloud(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")
        monkeypatch.delenv("OMNIX_EMBED_BASE_URL", raising=False)
        monkeypatch.delenv("OMNIX_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("OMNIX_OPENROUTER_BASE_URL", raising=False)

        from cograph_client.nlp import embed_client

        monkeypatch.setattr(
            embed_client,
            "_embeddings_url",
            lambda: "https://openrouter.ai/api/v1/embeddings",
        )

        with pytest.raises(OfflineModeError):
            await embed_client.embed_texts(["hello"], api_key="sk-test")

    @pytest.mark.asyncio
    async def test_wikidata_lookup_blocks(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")

        from cograph_client.enrichment.sources.wikidata import WikidataAdapter

        src = WikidataAdapter()
        with pytest.raises(OfflineModeError) as ei:
            await src.lookup("Ada Lovelace", "country", {})
        assert "Wikidata" in str(ei.value) or "wikidata" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_page_fetch_degrades_offline(self, monkeypatch):
        monkeypatch.setenv("OMNIX_OFFLINE", "1")

        from cograph_client.retrieval.fetch import StaticHttpFetcher

        page = await StaticHttpFetcher().fetch("https://example.com/page")
        assert page.ok is False
        assert "Offline mode" in (page.error or "")
