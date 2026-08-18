"""Process bootstrap configuration.

Each deployable reads its own environment prefix and its own database URL, so a process
cannot be required to hold another deployable's credentials in order to boot. Values may
come from a ``.env`` file (see ``.env.example``).

Knobs that must change without a redeploy — poll cadence, FR-C2 threshold, FR-K2 window,
source enable/disable — belong in the RFC §9 runtime config plane, not here.
"""

from pydantic import Field, PostgresDsn, SecretStr
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

    #: Requests per minute per consumer, counted separately for the two paths so a polling
    #: loop cannot consume a caller's budget for submitting work.
    inference_rate_limit_per_minute: int = Field(default=600, gt=0)
    poll_rate_limit_per_minute: int = Field(default=1200, gt=0)

    #: How long a finished job and its idempotency key remain readable. The published
    #: guarantee is 24 hours; a stub deployment may shorten it so a consumer can exercise
    #: the expired state without waiting a day.
    retention_hours: int = Field(default=24, gt=0)


class AdminSettings(_Base):
    """The admin surface's credential. Environment prefix ``MERIDIAN_ADMIN_``.

    Its own class rather than a field on ``AppSettings`` because the two are required of
    different processes. ``AppSettings`` is loaded by anything that reaches the application's
    database — migrations, the pipeline, tests — and none of those serve the admin surface. A
    token declared there would have to be supplied to run a migration, which is how a
    credential ends up in a deploy job that has no use for it.

    Splitting it also makes the scope enforceable rather than promised: a process that never
    constructs this class never holds the token, which is not something a default on a shared
    class can offer.
    """

    model_config = SettingsConfigDict(env_prefix="MERIDIAN_ADMIN_")

    #: Required, no default: a web process without one refuses to boot rather than serving an
    #: unguarded surface that can change rights_level and enabled.
    #:
    #: One shared secret, not a per-person identity. It makes the surface unreachable without
    #: the credential and does not attribute a change to anyone — the boundary recorded in RFC
    #: §9 (rev 19), adequate while one operator holds it.
    #:
    #: There is deliberately no Platform counterpart: that service is stateless and holds
    #: nothing to administer.
    #:
    #: SecretStr keeps it out of logs and tracebacks via repr. Compare with
    #: secrets.compare_digest, never ==. Generate with: openssl rand -hex 32
    token: SecretStr = Field(min_length=32)


def load_app() -> AppSettings:
    """Read the application's settings from the environment.

    Use this rather than ``AppSettings()``: type checkers see the required fields and not the
    environment that supplies them.
    """
    return AppSettings()  # type: ignore[call-arg]


def load_platform() -> PlatformSettings:
    """Read the Platform's settings from the environment."""
    return PlatformSettings()  # type: ignore[call-arg]


def load_admin() -> AdminSettings:
    """Read the admin surface's credential from the environment."""
    return AdminSettings()  # type: ignore[call-arg]


__all__ = [
    "AdminSettings",
    "AppSettings",
    "PlatformSettings",
    "load_admin",
    "load_app",
    "load_platform",
]
