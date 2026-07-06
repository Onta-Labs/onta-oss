import json
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neptune_endpoint: str = "http://localhost:8182"
    graph_backend: str = "neptune"  # "neptune" or "fuseki"
    api_keys: str = '{}'  # empty = open access, no auth required
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    cerebras_api_key: str = ""
    function_arns: str = "{}"
    log_level: str = "INFO"
    embeddings_s3_bucket: str = ""
    embeddings_s3_prefix: str = "omnix/embeddings"
    embeddings_top_k: int = 15

    # Optional Postgres DSN (env OMNIX_DATABASE_URL). When set, the durable
    # PostgresJobStore is used for tracked jobs; when empty, jobs are kept in
    # process memory. This is a GENERIC DSN — any Postgres (local, Aurora, Neon,
    # Supabase, ...) — and intentionally carries no cloud-provider identifiers.
    database_url: str = ""

    # Optional auth plugin: a dotted "module.path:callable" that will be
    # imported at app startup. The callable is invoked with no arguments
    # and is expected to register an external API key verifier via
    # omnix.auth.api_keys.register_external_verifier. Keeps omnix-oss
    # vendor-neutral while allowing downstream deployments to plug in
    # their own key verification backend (Clerk, WorkOS, custom, ...).
    auth_plugin: str = ""

    # Optional enrichment plugin: a dotted "module.path:callable" that will
    # be imported at app startup. The callable is invoked with no arguments
    # and is expected to register paid source adapters via
    # cograph_client.enrichment.sources.base.register_adapter and override
    # tier→chain mappings via cograph_client.enrichment.tiers.register_tier.
    # Keeps cograph-oss vendor-neutral while allowing downstream deployments
    # to plug in proprietary adapters (web search, LLM, GS1, ...).
    enrichment_plugin: str = ""

    # Optional governance plugin (COG-56): a dotted "module.path:callable"
    # imported at app startup. The callable is invoked with no arguments and
    # is expected to register a mapping-shape judge panel via
    # cograph_client.resolver.governance.register_governance_panel. Without
    # it, mapping-shape proposals are recorded pending (tenant-layer-only).
    governance_plugin: str = ""

    # Optional router plugins: a comma-separated list of dotted
    # "module.path:callable" entries imported at app startup. Each callable is
    # invoked with the FastAPI app instance so it can mount additional routers
    # via app.include_router(...). Keeps cograph-oss vendor-neutral while
    # letting downstream deployments attach proprietary endpoints (e.g. the
    # premium ontology recommender). Without it, only the OSS routers are
    # mounted.
    router_plugins: str = ""

    # Optional web-source plugin: a dotted "module.path:callable" imported at
    # app startup. The callable is invoked with no arguments and is expected to
    # register a web-discovery provider via
    # cograph_client.web_sources.base.register_web_source. Without it, the
    # "discover" agent intent stays dormant (plan() returns a "not enabled"
    # message). The OSS dev stub registers via
    # "cograph_client.web_sources.stub:register"; a downstream deployment points
    # this at its paid provider (Exa/Perplexity fan-out) with no OSS change.
    web_source_plugin: str = ""

    # Optional API-source-registry plugin (ONTA-194): a dotted
    # "module.path:callable" imported at app startup. The callable is invoked
    # with no arguments and is expected to contribute the premium
    # "global_enhanced" catalog overlay via
    # cograph_client.api_registry.register_api_source_layer. Without it, only
    # the OSS "global_public" seed catalog is loaded. Keeps cograph-oss
    # vendor-neutral while letting a downstream deployment ship curated premium
    # (paid/licensed) API entries with no OSS change.
    api_registry_plugin: str = ""

    # Optional secret-cipher plugin (ONTA-2xx): a dotted "module.path:callable"
    # imported at app startup. The callable is invoked with no arguments and is
    # expected to register a SecretCipher via
    # cograph_client.api_registry.register_secret_cipher — e.g. an AWS-KMS
    # data-key cipher in our deploy. Without it, tenant-custom API credentials
    # are encrypted with the OSS default LocalAesGcmCipher keyed by
    # OMNIX_SECRETS_KEY (below). Keeps cograph-oss vendor-neutral: a self-hoster
    # needs only OMNIX_SECRETS_KEY; a cloud deploy points this at its KMS cipher.
    secrets_cipher_plugin: str = ""

    # Optional local symmetric key for the OSS default secret cipher
    # (LocalAesGcmCipher). When set (and no cipher plugin is registered),
    # tenant-custom API credentials are envelope-encrypted at rest with AES-256-GCM
    # under this key. Accepts base64/base64url (16/24/32 bytes) or a raw
    # passphrase (stretched to 32 bytes via SHA-256). Empty ⇒ no default cipher,
    # and the routes REFUSE to store a secret (fail closed) rather than store it
    # in the clear. env: OMNIX_SECRETS_KEY.
    secrets_key: str = ""

    def get_api_keys_map(self) -> dict[str, str]:
        return json.loads(self.api_keys)

    def get_function_arns_map(self) -> dict[str, str]:
        return json.loads(self.function_arns)

    model_config = {"env_prefix": "OMNIX_"}


settings = Settings()
