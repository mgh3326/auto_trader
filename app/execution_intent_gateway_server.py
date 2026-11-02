"""ROB-1461 — the only process that serves the intent gateway route.

It is a separate app on purpose. ``app.main`` sits behind the public proxy, and a
route there would be reachable from outside with only a token in the way. This
app mounts nothing else, has no docs pages, and is started on loopback by
``scripts/run_execution_intent_gateway.py``.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.routers import execution_intent_gateway


def create_app() -> FastAPI:
    app = FastAPI(
        title="Execution intent gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.include_router(execution_intent_gateway.router)
    return app
