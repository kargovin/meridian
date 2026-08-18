"""The Platform boots and serves without any of the application's configuration."""

import pytest
from fastapi.testclient import TestClient
from meridian_config import PlatformSettings
from meridian_platform.main import create_app

PLATFORM_URL = "postgresql+psycopg://platform:platform@localhost:5433/platform"


@pytest.fixture
def client() -> TestClient:
    """A client on an app built from settings passed in, never read from the environment."""
    settings = PlatformSettings(database_url=PLATFORM_URL)  # type: ignore[arg-type]
    return TestClient(create_app(settings))


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_absent_from_the_published_schema(client: TestClient) -> None:
    """The generated document is the contract; an operational probe is not part of it."""
    assert "/health" not in client.app.openapi()["paths"]  # type: ignore[attr-defined]


def test_the_app_builds_without_the_application_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The point of the settings split: this process holds no credentials for the app's data."""
    monkeypatch.delenv("MERIDIAN_DATABASE_URL", raising=False)
    monkeypatch.setenv("MERIDIAN_PLATFORM_DATABASE_URL", PLATFORM_URL)

    app = create_app()

    assert str(app.state.settings.database_url) == PLATFORM_URL
