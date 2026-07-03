"""Web-discovery providers — turn an NL query into ingestable records.

See :mod:`cograph_client.web_sources.base` for the provider protocol and
registry. OSS ships no provider; a downstream deployment registers a paid one
(Exa/Perplexity/Parallel fan-out) at boot, the same way it registers paid
enrichment adapters.
"""

from cograph_client.web_sources.base import (
    DiscoverResult,
    WebSourceProvider,
    get_web_source,
    get_web_source_for_kind,
    has_kind_specialized_provider,
    list_web_sources,
    provider_cost,
    register_web_source,
    reset_web_sources,
)

__all__ = [
    "DiscoverResult",
    "WebSourceProvider",
    "get_web_source",
    "get_web_source_for_kind",
    "has_kind_specialized_provider",
    "list_web_sources",
    "provider_cost",
    "register_web_source",
    "reset_web_sources",
]
