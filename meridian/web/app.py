"""Application factory.

Run with ``uvicorn meridian.web.app:create_app --factory``.
"""

from fastapi import FastAPI
from meridian_config import AdminSettings, AppSettings, load_admin, load_app

from meridian.db.session import create_engine, session_factory
from meridian.web.admin import router as admin_router


def create_app(
    settings: AppSettings | None = None,
    admin: AdminSettings | None = None,
) -> FastAPI:
    """Build the application.

    Configuration is read here rather than at module level, so importing this module requires
    nothing of the environment — which is what lets a boundary check or a schema dump run in a
    process that has none. Passing both settings objects builds an app without reading the
    environment at all; omitting them reads it, which is what a deployed process wants.

    Loading ``AdminSettings`` here is what makes the credential a boot requirement: a web
    process started without ``MERIDIAN_ADMIN_TOKEN`` fails at startup rather than serving an
    unguarded surface that can change rights_level and enabled.
    """
    settings = settings or load_app()
    admin = admin or load_admin()

    app = FastAPI(title="Meridian News", version="0.1.0")
    app.state.settings = settings
    app.state.session_factory = session_factory(create_engine(settings))
    app.state.admin_token = admin.token.get_secret_value()
    app.include_router(admin_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Liveness probe.

        Unauthenticated on purpose: it reports whether the process is up and reveals nothing
        about the registry. A probe behind a credential is a probe the orchestrator cannot
        use.
        """
        return {"status": "ok"}

    return app
