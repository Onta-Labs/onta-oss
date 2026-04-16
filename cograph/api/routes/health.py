from fastapi import APIRouter, Depends

from cograph.api.deps import get_neptune_client
from cograph.graph.client import NeptuneClient

router = APIRouter()


@router.get("/health")
async def health(client: NeptuneClient = Depends(get_neptune_client)):
    neptune_ok = await client.health()
    status = "healthy" if neptune_ok else "degraded"
    return {"status": status, "neptune": neptune_ok}
