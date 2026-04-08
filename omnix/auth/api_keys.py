from dataclasses import dataclass

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from omnix.config import settings

api_key_header = APIKeyHeader(name="X-API-Key")


@dataclass
class TenantContext:
    tenant_id: str
    api_key: str


def get_tenant(api_key: str = Security(api_key_header)) -> TenantContext:
    keys_map = settings.get_api_keys_map()
    tenant_id = keys_map.get(api_key)
    if tenant_id is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return TenantContext(tenant_id=tenant_id, api_key=api_key)
