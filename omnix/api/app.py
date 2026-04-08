from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from omnix.api.middleware import RequestLoggingMiddleware
from omnix.api.rate_limit import limiter
from omnix.api.routes import ask, functions, health, ingest, knowledge_graphs, ontology, query, triples
from omnix.config import settings
from omnix.graph.client import NeptuneClient
from omnix.logging import setup_logging

logger = structlog.stdlib.get_logger("omnix.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.log_level)
    logger.info("starting", neptune_endpoint=settings.neptune_endpoint)
    app.state.neptune_client = NeptuneClient(settings.neptune_endpoint, backend=settings.graph_backend)
    yield
    await app.state.neptune_client.close()
    logger.info("shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Omnix",
        description="Living Knowledge Graph Platform",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(health.router, tags=["health"])
    app.include_router(triples.router, tags=["triples"])
    app.include_router(query.router, tags=["query"])
    app.include_router(functions.router, tags=["functions"])
    app.include_router(ask.router, tags=["ask"])
    app.include_router(ontology.router, tags=["ontology"])
    app.include_router(ingest.router, tags=["ingest"])
    app.include_router(knowledge_graphs.router, tags=["knowledge_graphs"])
    return app


app = create_app()
