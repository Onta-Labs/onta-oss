from fastapi import Request

from cograph_client.graph.client import NeptuneClient


def get_neptune_client(request: Request) -> NeptuneClient:
    return request.app.state.neptune_client
