"""Application factory.

Run with ``uvicorn meridian.web.app:create_app --factory``.
"""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Build the application.

    Nothing is read from the environment at import time, so importing this module requires
    only that the code loads — which is what lets a schema dump or a boundary check run
    without a database.
    """
    app = FastAPI(title="Meridian News", version="0.1.0")

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    return app
