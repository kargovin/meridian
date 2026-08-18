"""Application factory.

Run with ``uvicorn meridian_platform.main:create_app --factory``.
"""

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from meridian_config import PlatformSettings, load_platform

from meridian_platform.db import create_engine, session_factory
from meridian_platform.errors import register_error_handlers
from meridian_platform.limits import RateLimiter
from meridian_platform.routes import router
from meridian_platform.worker import BackgroundLoops


def create_app(settings: PlatformSettings | None = None, background: bool = True) -> FastAPI:
    """Build the service.

    Configuration is read here rather than at module level so that importing this module
    requires nothing of the environment: an import that reads settings cannot be performed by
    a process that has none, which rules out schema dumps and any check that only wants to
    load the code. Passing ``settings`` lets a caller build an app without the environment at
    all; omitting it reads the environment, which is what a deployed process wants.

    ``background=False`` leaves the queue and retention loops unstarted, for a caller that
    drives ``jobs.process_next`` and ``retention.sweep`` itself.
    """
    settings = settings or load_platform()
    engine = create_engine(settings)
    sessions = session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        loops = (
            BackgroundLoops(sessions, dt.timedelta(hours=settings.retention_hours))
            if background
            else None
        )
        if loops is not None:
            loops.start()
        try:
            yield
        finally:
            if loops is not None:
                loops.stop()

    app = FastAPI(title="Summarization & Classification Platform", version="1", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = sessions
    app.state.inference_limiter = RateLimiter(settings.inference_rate_limit_per_minute)
    app.state.poll_limiter = RateLimiter(settings.poll_rate_limit_per_minute)
    register_error_handlers(app)
    app.include_router(router)

    _publish_one_error_shape(app)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Liveness probe.

        Excluded from the schema: the generated OpenAPI document is the contract consumers
        build against, and an operational probe is not part of what is promised to them.
        """
        return {"status": "ok"}

    return app


def _publish_one_error_shape(app: FastAPI) -> None:
    """Drop FastAPI's automatic 422 from the document.

    Request validation is answered as 400 with the locked envelope (see ``errors``), so a
    published 422 with FastAPI's own ``{"detail": [...]}`` body would describe a response
    this service never sends and put a second error shape in a frozen contract.
    """

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            for path in schema.get("paths", {}).values():
                for operation in path.values():
                    operation.get("responses", {}).pop("422", None)
            for name in ("HTTPValidationError", "ValidationError"):
                schema.get("components", {}).get("schemas", {}).pop(name, None)
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
