"""Settings resolution. No database — nothing here opens a connection.

Every instantiation passes ``_env_file=None``. The classes read ``.env`` by default and that
file is gitignored, so it exists on a developer machine and not in CI: a test that leaves it
enabled reads ambient values in one place and not the other, and asserts something different
in each.
"""

import pytest
from meridian_config import AdminSettings, AppSettings, PlatformSettings
from pydantic import ValidationError

APP_URL = "postgresql+psycopg://meridian:meridian@localhost:5433/meridian"
PLATFORM_URL = "postgresql+psycopg://platform:platform@localhost:5433/platform"
TOKEN = "0" * 32


def test_platform_loads_without_the_application_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Platform boots in a process holding no credentials for the application's data."""
    monkeypatch.delenv("MERIDIAN_DATABASE_URL", raising=False)
    monkeypatch.setenv("MERIDIAN_PLATFORM_DATABASE_URL", PLATFORM_URL)

    assert str(PlatformSettings(_env_file=None).database_url) == PLATFORM_URL  # type: ignore[call-arg]


def test_application_still_requires_its_own_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """One deployable was freed of the requirement, not both."""
    monkeypatch.delenv("MERIDIAN_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        AppSettings(_env_file=None)  # type: ignore[call-arg]


def test_platform_does_not_fall_back_to_the_application_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separate prefixes, not two names for one variable.

    With only the application's variable set the Platform must fail to start, rather than
    quietly reaching for the database it is not allowed to read.
    """
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", APP_URL)
    monkeypatch.delenv("MERIDIAN_PLATFORM_DATABASE_URL", raising=False)

    with pytest.raises(ValidationError):
        PlatformSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_two_resolve_to_different_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a copied env_prefix, which leaves both classes reading one variable."""
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", APP_URL)
    monkeypatch.setenv("MERIDIAN_PLATFORM_DATABASE_URL", PLATFORM_URL)

    app = AppSettings(_env_file=None)  # type: ignore[call-arg]
    platform = PlatformSettings(_env_file=None)  # type: ignore[call-arg]

    assert str(app.database_url) == APP_URL
    assert str(platform.database_url) == PLATFORM_URL


def test_running_a_migration_does_not_require_the_admin_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason the credential is its own class.

    ``migrations/env.py`` calls ``load_app()``. If the token lived on ``AppSettings`` the
    deploy job that runs ``alembic upgrade head`` would have to be handed the admin secret to
    apply a schema change, which has nothing to do with the admin surface.
    """
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", APP_URL)
    monkeypatch.delenv("MERIDIAN_ADMIN_TOKEN", raising=False)

    assert str(AppSettings(_env_file=None).database_url) == APP_URL  # type: ignore[call-arg]


def test_the_admin_surface_will_not_start_without_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refusing to boot is the point: the alternative is an unguarded surface that can set
    rights_level and enabled."""
    monkeypatch.delenv("MERIDIAN_ADMIN_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        AdminSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_token_reads_its_own_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a prefix that resolves to MERIDIAN_TOKEN or MERIDIAN_ADMIN_ADMIN_TOKEN."""
    monkeypatch.setenv("MERIDIAN_ADMIN_TOKEN", TOKEN)

    assert AdminSettings(_env_file=None).token.get_secret_value() == TOKEN  # type: ignore[call-arg]


def test_a_degenerate_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Required alone still admits MERIDIAN_ADMIN_TOKEN=x."""
    monkeypatch.setenv("MERIDIAN_ADMIN_TOKEN", "x")

    with pytest.raises(ValidationError):
        AdminSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_token_does_not_appear_in_a_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings object reaches logs and tracebacks; the secret must not ride along."""
    monkeypatch.setenv("MERIDIAN_ADMIN_TOKEN", TOKEN)

    settings = AdminSettings(_env_file=None)  # type: ignore[call-arg]

    assert TOKEN not in repr(settings)
    assert TOKEN not in str(settings.token)
