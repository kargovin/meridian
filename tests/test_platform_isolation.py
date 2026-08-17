"""The A1 boundary as PostgreSQL enforces it.

Everything else guarding this boundary — the ruff rule, the subprocess import check — is our
own tooling and stops working the moment someone deletes it. These hold at the server.
"""

import pytest
import sqlalchemy as sa
from meridian_config import load_app, load_platform
from meridian_platform.db import Base as PlatformBase

from meridian.db.models import Base as AppBase

pytestmark = pytest.mark.postgres


def test_the_platform_role_reaches_its_own_database(platform_migrated: sa.Engine) -> None:
    """The control for the test below: a role that cannot connect anywhere proves nothing."""
    with platform_migrated.connect() as conn:
        assert conn.execute(sa.text("SELECT current_user")).scalar() == "platform"


def test_the_platform_role_cannot_reach_the_application_database(
    platform_migrated: sa.Engine, migrated: sa.Engine
) -> None:
    app_database = sa.engine.make_url(str(load_app().database_url)).database
    trespass = platform_migrated.url.set(database=f"{app_database}_test")

    engine = sa.create_engine(trespass)
    try:
        with pytest.raises(sa.exc.OperationalError) as caught:
            engine.connect()
    finally:
        engine.dispose()

    assert "permission denied for database" in str(caught.value)


def test_the_two_deployables_are_pointed_at_different_databases() -> None:
    """Pointing both at one database is what makes each tree drop the other's tables."""
    app = sa.engine.make_url(str(load_app().database_url))
    platform = sa.engine.make_url(str(load_platform().database_url))

    assert (app.host, app.port, app.database) != (platform.host, platform.port, platform.database)


def test_the_metadata_objects_are_disjoint() -> None:
    assert AppBase.metadata is not PlatformBase.metadata
    assert set(AppBase.metadata.tables) & set(PlatformBase.metadata.tables) == set()
