"""The application factory boots and serves its probe."""

from fastapi.testclient import TestClient

from meridian.web.app import create_app


def test_create_app_needs_nothing_from_the_environment() -> None:
    """Importing and building the app must not require configuration or a database.

    This is what lets a boundary check or a schema dump run in a bare process. It is
    asserted rather than assumed because the failure is silent: a factory that grows an
    environment read still passes every test that happens to run with one set.
    """
    assert create_app() is not None


def test_health_answers() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_absent_from_the_schema() -> None:
    """An operational probe is not part of what the app publishes."""
    assert "/health" not in create_app().openapi()["paths"]
