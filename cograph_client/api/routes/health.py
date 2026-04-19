from fastapi import APIRouter, Depends

from cograph_client.api.deps import get_neptune_client
from cograph_client.graph.client import NeptuneClient

router = APIRouter()


@router.get("/health")
async def health(client: NeptuneClient = Depends(get_neptune_client)):
    neptune_ok = await client.health()
    status = "healthy" if neptune_ok else "degraded"
    return {"status": status, "neptune": neptune_ok}
