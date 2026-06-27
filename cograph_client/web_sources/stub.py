"""Dev/test stub web-source provider — canned rows, zero network, zero spend.

This is NOT the real discovery provider. It exists so you can exercise the whole
web-discovery rail locally (plan card → confirm → ingest → rows in the Explorer)
without wiring a paid provider. Enable it by pointing the web-source plugin at
this module's :func:`register`::

    export OMNIX_WEB_SOURCE_PLUGIN=cograph_client.web_sources.stub:register

It returns a small believable table for an "OpenRouter models" style query, and a
generic synthesized table for anything else, so schema inference + ingest have
well-formed records to work with. Replace it with a real provider (Exa/Perplexity
fan-out) registered the same way for actual web data.
"""

from __future__ import annotations

from urllib.parse import urlparse

import structlog

from cograph_client.web_sources.base import DiscoverResult, register_web_source

logger = structlog.stdlib.get_logger("cograph.web_sources.stub")

# A small, believable OpenRouter-style catalogue (illustrative values, not live).
_OPENROUTER_MODELS: list[dict[str, str]] = [
    {"name": "anthropic/claude-opus-4-8", "provider": "Anthropic", "context_length": "200000", "input_price_per_1m_usd": "15.00", "modality": "text+vision"},
    {"name": "anthropic/claude-sonnet-4-6", "provider": "Anthropic", "context_length": "200000", "input_price_per_1m_usd": "3.00", "modality": "text+vision"},
    {"name": "openai/gpt-5", "provider": "OpenAI", "context_length": "400000", "input_price_per_1m_usd": "10.00", "modality": "text+vision"},
    {"name": "openai/gpt-5-mini", "provider": "OpenAI", "context_length": "400000", "input_price_per_1m_usd": "2.00", "modality": "text"},
    {"name": "google/gemini-2.5-flash", "provider": "Google", "context_length": "1000000", "input_price_per_1m_usd": "0.30", "modality": "text+vision"},
    {"name": "google/gemini-2.5-pro", "provider": "Google", "context_length": "1000000", "input_price_per_1m_usd": "1.25", "modality": "text+vision"},
    {"name": "meta-llama/llama-4-70b", "provider": "Meta", "context_length": "128000", "input_price_per_1m_usd": "0.60", "modality": "text"},
    {"name": "mistralai/mistral-large-3", "provider": "Mistral", "context_length": "128000", "input_price_per_1m_usd": "2.00", "modality": "text"},
    {"name": "deepseek/deepseek-v3", "provider": "DeepSeek", "context_length": "131072", "input_price_per_1m_usd": "0.27", "modality": "text"},
    {"name": "cohere/command-a", "provider": "Cohere", "context_length": "256000", "input_price_per_1m_usd": "2.50", "modality": "text"},
]

_OPENROUTER_SOURCE = "https://openrouter.ai/models"


class StubWebSource:
    """Canned provider. Free, deterministic, query-aware enough to demo.

    Also supports the URL-targeted mode (``supports_urls=True``) so the
    URL-extraction rail can be exercised end-to-end without a paid scraper: when
    ``discover`` is called with ``urls``, it returns a few canned rows whose
    provenance maps onto those URLs and whose ``sources`` are the URLs. It is NOT
    ``url_only`` — it keeps serving plain query discovery too."""

    name = "stub"
    is_paid = False
    cost_per_call = 0.0
    supports_urls = True
    url_only = False

    async def discover(
        self,
        query: str,
        *,
        sample: bool,
        max_rows: int,
        hint_columns: list[str] | None,
        context: dict,
        urls: list[str] | None = None,
    ) -> DiscoverResult:
        if urls:
            return self._discover_urls(query, sample, max_rows, hint_columns, urls)

        q = (query or "").lower()
        if "openrouter" in q or ("model" in q and "list" in q):
            rows = _OPENROUTER_MODELS
            source = _OPENROUTER_SOURCE
        else:
            rows = _synthesize(query)
            source = "stub://canned"

        total = len(rows)
        take = min(max_rows, 5) if sample else max_rows
        out = [_project(r, hint_columns) for r in rows[:take]]
        logger.info(
            "stub_discover",
            query=query, sample=sample, returned=len(out), total=total,
            columns=hint_columns,
        )
        return DiscoverResult(
            rows=out,
            provenance={r.get("name", str(i)): source for i, r in enumerate(out)},
            sources=[source],
            is_partial=take < total,
            estimated_total=total,
        )

    def _discover_urls(
        self,
        query: str,
        sample: bool,
        max_rows: int,
        hint_columns: list[str] | None,
        urls: list[str],
    ) -> DiscoverResult:
        """URL-targeted extraction stub: synthesize one canned row per URL so the
        rail can be tested. ``sources`` are the input URLs and ``provenance`` maps
        each row's natural key onto the URL it 'came from'."""
        rows: list[dict[str, str]] = []
        provenance: dict[str, str] = {}
        take = min(max_rows, 5) if sample else max_rows
        for i, url in enumerate(urls[:take]):
            row = {
                "name": f"{_host(url) or 'page'} #{i + 1}",
                "description": f"Stub record extracted from {url}.",
                "url": url,
            }
            projected = _project(row, hint_columns)
            rows.append(projected)
            provenance[projected.get("name", row["name"])] = url
        logger.info(
            "stub_discover_urls",
            query=query, sample=sample, returned=len(rows), urls=len(urls),
            columns=hint_columns,
        )
        return DiscoverResult(
            rows=rows,
            provenance=provenance,
            sources=list(urls),
            is_partial=take < len(urls),
            estimated_total=len(urls),
        )


# Aliases so a confirmed attribute name maps onto a canned field when close.
_ALIASES: dict[str, str] = {
    "input_price": "input_price_per_1m_usd",
    "price": "input_price_per_1m_usd",
    "pricing": "input_price_per_1m_usd",
    "cost": "input_price_per_1m_usd",
    "context": "context_length",
    "context_window": "context_length",
}
# Providers we treat as open-source for the canned open_source attribute.
_OPEN_PROVIDERS = {"meta", "mistral", "deepseek"}


def _project(row: dict[str, str], hint_columns: list[str] | None) -> dict[str, str]:
    """Project a canned row onto the requested columns. Exact match wins, then a
    small alias map, then a couple of derived values, else "unknown" — so the row
    always carries the confirmed schema with well-formed values."""
    if not hint_columns:
        return dict(row)
    out: dict[str, str] = {}
    for col in hint_columns:
        if col in row:
            out[col] = row[col]
        elif col in _ALIASES and _ALIASES[col] in row:
            out[col] = row[_ALIASES[col]]
        elif col == "open_source" and "provider" in row:
            out[col] = "yes" if row["provider"].lower() in _OPEN_PROVIDERS else "no"
        else:
            out[col] = "unknown"
    return out


def _host(url: str) -> str:
    """Hostname of a URL with a leading ``www.`` dropped; '' if unparseable."""
    try:
        netloc = urlparse(url).netloc
    except Exception:  # noqa: BLE001
        netloc = ""
    host = (netloc or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def _synthesize(query: str) -> list[dict[str, str]]:
    """Well-formed generic rows for any non-OpenRouter query, so inference works."""
    subject = (query or "item").strip()[:40] or "item"
    return [
        {
            "name": f"{subject} #{i}",
            "description": f"Stub record {i} for “{subject}”.",
            "url": f"https://example.com/{i}",
        }
        for i in range(1, 7)
    ]


def register() -> None:
    """Plugin entry point — register the stub provider. See the module docstring."""
    register_web_source(StubWebSource())
    logger.info("stub_web_source_registered")
