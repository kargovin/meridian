"""The application factory."""

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import AdminSettings, AppSettings
from pydantic import ValidationError

from meridian.web.app import create_app

TOKEN = "t" * 32


def _settings(engine: sa.Engine) -> tuple[AppSettings, AdminSettings]:
    return (
        AppSettings.model_validate(
            {"database_url": engine.url.render_as_string(hide_password=False)}
        ),
        AdminSettings.model_validate({"token": TOKEN}),
    )


def test_building_the_app_can_avoid_the_environment(app_migrated: sa.Engine) -> None:
    """Passing both settings objects builds an app without reading any variable.

    Asserted because the failure is silent: a factory that grows an environment read still
    passes every test that happens to run with one set.
    """
    settings, admin = _settings(app_migrated)

    assert create_app(settings=settings, admin=admin) is not None


def test_a_web_process_without_a_token_refuses_to_start(
    app_migrated: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The credential is a boot requirement, not a runtime check.

    Starting and serving the registry unguarded — even briefly, even to return 401s from a
    check somebody later makes conditional — is the state this rules out.
    """
    settings, _ = _settings(app_migrated)
    monkeypatch.delenv("MERIDIAN_ADMIN_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        create_app(settings=settings)


def test_health_answers_without_a_credential(app_migrated: sa.Engine) -> None:
    """A probe behind a credential is a probe the orchestrator cannot use."""
    settings, admin = _settings(app_migrated)

    with TestClient(create_app(settings=settings, admin=admin)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_absent_from_the_schema(app_migrated: sa.Engine) -> None:
    """An operational probe is not part of what the app publishes."""
    settings, admin = _settings(app_migrated)

    assert "/health" not in create_app(settings=settings, admin=admin).openapi()["paths"]
