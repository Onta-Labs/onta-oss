import json
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    neptune_endpoint: str = "http://localhost:8182"
    graph_backend: str = "neptune"  # "neptune" or "fuseki"
    api_keys: str = '{"dev-key-001": "demo-tenant"}'
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    cerebras_api_key: str = ""
    function_arns: str = "{}"
    log_level: str = "INFO"
    embeddings_s3_bucket: str = ""
    embeddings_s3_prefix: str = "omnix/embeddings"
    embeddings_top_k: int = 15

    def get_api_keys_map(self) -> dict[str, str]:
        return json.loads(self.api_keys)

    def get_function_arns_map(self) -> dict[str, str]:
        return json.loads(self.function_arns)

    model_config = {"env_prefix": "OMNIX_"}


settings = Settings()
