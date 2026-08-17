"""Application factory.

Run with ``uvicorn meridian_platform.main:create_app --factory``.
"""

from fastapi import FastAPI
from meridian_config import PlatformSettings, load_platform

from meridian_platform.errors import register_error_handlers
from meridian_platform.routes import router


def create_app(settings: PlatformSettings | None = None) -> FastAPI:
    """Build the service.

    Configuration is read here rather than at module level so that importing this module
    requires nothing of the environment: an import that reads settings cannot be performed by
    a process that has none, which rules out schema dumps and any check that only wants to
    load the code. Passing ``settings`` lets a caller build an app without the environment at
    all; omitting it reads the environment, which is what a deployed process wants.
    """
    settings = settings or load_platform()

    app = FastAPI(title="Summarization & Classification Platform", version="1")
    app.state.settings = settings
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
