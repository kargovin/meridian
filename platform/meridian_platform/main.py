"""Application factory.

Run with ``uvicorn meridian_platform.main:create_app --factory``.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from meridian_config import PlatformSettings, load_platform

from meridian_platform.db import create_engine, session_factory
from meridian_platform.errors import register_error_handlers
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
        loops = BackgroundLoops(sessions) if background else None
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
    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Liveness probe.

        Excluded from the schema: the generated OpenAPI document is the contract consumers
        build against, and an operational probe is not part of what is promised to them.
        """
        return {"status": "ok"}

    return app
