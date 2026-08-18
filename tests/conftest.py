"""Test fixtures.

Everything here runs against a real PostgreSQL in a database of its own (``<db>_test``),
created on demand and migrated with Alembic rather than ``metadata.create_all`` — the
migration is part of what is under test.
"""

import os
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from meridian_config import load_app, load_platform
from meridian_platform.bootstrap import ensure_platform_database
from meridian_platform.db import Base as PlatformBase
from sqlalchemy.orm import Session

from meridian.db.models import Base


@pytest.fixture(scope="session")
def app_alembic_config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="session")
def app_engine() -> Iterator[sa.Engine]:
    """An engine on a dedicated test database, created if it does not exist."""
    url = sa.engine.make_url(str(load_app().database_url))
    test_url = url.set(database=f"{url.database}_test")

    admin = sa.create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": test_url.database},
            ).scalar()
            if not exists:
                conn.execute(sa.text(f'CREATE DATABASE "{test_url.database}"'))
    except sa.exc.OperationalError as exc:
        target = url.render_as_string(hide_password=True)
        # Skipping is a convenience for a laptop with no database running. In CI it would
        # turn every acceptance criterion into a silent no-op and still exit 0.
        if os.environ.get("CI"):
            pytest.fail(f"no PostgreSQL at {target}: {exc}", pytrace=False)
        pytest.skip(f"no PostgreSQL at {target}: {exc}")
    finally:
        admin.dispose()

    eng = sa.create_engine(test_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def app_migrated(app_engine: sa.Engine, app_alembic_config: Config) -> Iterator[sa.Engine]:
    """The test database at head, rebuilt from scratch once per session."""
    with app_engine.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        command.downgrade(app_alembic_config, "base")
        command.upgrade(app_alembic_config, "head")
    yield app_engine


@pytest.fixture
def app_session(app_migrated: sa.Engine) -> Iterator[Session]:
    """A session over empty tables.

    Truncates rather than rolling back: the claim path commits, so a wrapping transaction
    would not survive the code under test.
    """
    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    with Session(app_migrated, expire_on_commit=False) as db:
        db.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        db.commit()
        yield db


@pytest.fixture(scope="session")
def platform_alembic_config() -> Config:
    return Config("platform/alembic.ini")


@pytest.fixture(scope="session")
def platform_engine(app_engine: sa.Engine) -> Iterator[sa.Engine]:
    """An engine on the Platform's test database, reached with the Platform's own role.

    Depends on ``app_engine`` so the application's test database exists before this provisioning
    revokes the default PUBLIC CONNECT grant on it.
    """
    app_url = sa.engine.make_url(str(load_app().database_url))
    platform_url = sa.engine.make_url(str(load_platform().database_url))
    test_url = platform_url.set(database=f"{platform_url.database}_test")

    ensure_platform_database(
        admin_url=app_url,
        database=str(test_url.database),
        role=str(test_url.username),
        password=str(test_url.password),
        protect_database=f"{app_url.database}_test",
    )

    eng = sa.create_engine(test_url)
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def platform_migrated(
    platform_engine: sa.Engine, platform_alembic_config: Config
) -> Iterator[sa.Engine]:
    """The Platform's test database at head, migrated by the Platform's own tree."""
    with platform_engine.begin() as conn:
        platform_alembic_config.attributes["connection"] = conn
        command.downgrade(platform_alembic_config, "base")
        command.upgrade(platform_alembic_config, "head")
    yield platform_engine


@pytest.fixture
def platform_session(platform_migrated: sa.Engine) -> Iterator[Session]:
    tables = ", ".join(f'"{name}"' for name in PlatformBase.metadata.tables)
    with Session(platform_migrated, expire_on_commit=False) as db:
        db.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        db.commit()
        yield db
