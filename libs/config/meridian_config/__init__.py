"""Process bootstrap configuration.

Each deployable reads its own environment prefix and its own database URL, so a process
cannot be required to hold another deployable's credentials in order to boot. Values may
come from a ``.env`` file (see ``.env.example``).

Knobs that must change without a redeploy — poll cadence, FR-C2 threshold, FR-K2 window,
source enable/disable — belong in the RFC §9 runtime config plane, not here.
"""

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Base(BaseSettings):
    """How settings are read: from ``.env``, ignoring unknown variables, immutable once read.

    No fields, deliberately. A field declared here is required of every class that inherits
    these conventions — it would resolve under each subclass's own prefix, but a deployable
    with no database of its own could not adopt the conventions without declaring one.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)


class AppSettings(_Base):
    """The Meridian application. Environment prefix ``MERIDIAN_``."""

    model_config = SettingsConfigDict(env_prefix="MERIDIAN_")

    #: Required, no default. Pass ``str(settings.database_url)`` to SQLAlchemy.
    database_url: PostgresDsn

    #: How long a claim on a work row stays valid before another worker may take it.
    #: Pass it to the claim call; don't read it as a constant at a call site.
    work_lease_seconds: int = Field(default=300, gt=0)


class PlatformSettings(_Base):
    """The Platform service. Environment prefix ``MERIDIAN_PLATFORM_``.

    A different database, reached with a different role. Nothing here reads
    ``MERIDIAN_DATABASE_URL``, so a Platform process never holds credentials for the
    application's data.
    """

    model_config = SettingsConfigDict(env_prefix="MERIDIAN_PLATFORM_")

    #: Required, no default. Pass ``str(settings.database_url)`` to SQLAlchemy.
    database_url: PostgresDsn


def load_app() -> AppSettings:
    """Read the application's settings from the environment.

    Use this rather than ``AppSettings()``: type checkers see the required fields and not the
    environment that supplies them.
    """
    return AppSettings()  # type: ignore[call-arg]


def load_platform() -> PlatformSettings:
    """Read the Platform's settings from the environment."""
    return PlatformSettings()  # type: ignore[call-arg]


__all__ = ["AppSettings", "PlatformSettings", "load_app", "load_platform"]
