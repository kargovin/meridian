"""Process bootstrap configuration.

Env vars are prefixed ``MERIDIAN_`` and may come from a ``.env`` file (see ``.env.example``).

Knobs that must change without a redeploy — poll cadence, FR-C2 threshold, FR-K2 window,
source enable/disable — belong in the RFC §9 runtime config plane, not here.
"""

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Immutable process configuration, read from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="MERIDIAN_",
        env_file=".env",
        extra="ignore",
        frozen=True,
    )

    #: Required, no default. Pass ``str(settings.database_url)`` to SQLAlchemy.
    database_url: PostgresDsn

    #: How long a claim on a work row stays valid before another worker may take it.
    #: Pass it to the claim call; don't read it as a constant at a call site.
    work_lease_seconds: int = Field(default=300, gt=0)


__all__ = ["Settings"]
