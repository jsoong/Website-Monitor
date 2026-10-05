"""FastAPI application factory for the engine's local API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI

from pagewatch import __version__
from pagewatch.engine.api import (
    routes_bookmarks,
    routes_checks,
    routes_folders,
    routes_health,
)
from pagewatch.engine.api.guard import GuardMiddleware

if TYPE_CHECKING:
    from pagewatch.engine.core import Engine


def create_app(engine: Engine) -> FastAPI:
    # Interactive docs are off: the schema is generated offline into docs/openapi.json.
    app = FastAPI(
        title="PageWatch engine",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.engine = engine
    app.include_router(routes_health.router)
    app.include_router(routes_folders.router)
    app.include_router(routes_bookmarks.router)
    app.include_router(routes_checks.router)
    app.add_middleware(GuardMiddleware, token=lambda: engine.token)
    return app
