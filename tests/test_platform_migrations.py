"""The Platform's migration and models must describe the same database."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from meridian_platform.db import Base

pytestmark = pytest.mark.postgres


def test_models_and_migration_agree(
    platform_migrated: sa.Engine, platform_alembic_config: Config
) -> None:
    with platform_migrated.begin() as conn:
        platform_alembic_config.attributes["connection"] = conn
        command.check(platform_alembic_config)


def test_downgrade_leaves_no_residue(
    platform_migrated: sa.Engine, platform_alembic_config: Config
) -> None:
    with platform_migrated.begin() as conn:
        platform_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(platform_alembic_config, "base")
            remaining = set(
                conn.execute(
                    sa.text(
                        "SELECT table_name FROM information_schema.tables"
                        " WHERE table_schema = 'public'"
                    )
                ).scalars()
            )
            assert remaining <= {"alembic_version"}
        finally:
            command.upgrade(platform_alembic_config, "head")


def test_every_entity_has_a_table(platform_migrated: sa.Engine) -> None:
    inspector = sa.inspect(platform_migrated)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())


def test_the_database_holds_no_application_tables(platform_migrated: sa.Engine) -> None:
    """A tree pointed at the wrong database drops what it does not recognise."""
    from meridian.db.models import Base as AppBase

    present = set(sa.inspect(platform_migrated).get_table_names())

    assert present & set(AppBase.metadata.tables) == set()


def test_constraints_follow_the_naming_convention(platform_migrated: sa.Engine) -> None:
    """A server-assigned name cannot be dropped by name in a later migration."""
    with platform_migrated.connect() as conn:
        names = set(
            conn.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE connamespace = 'public'::regnamespace"
                    " AND conrelid::regclass::text <> 'alembic_version'"
                )
            ).scalars()
        )
    assert names
    unconventional = sorted(n for n in names if not n.startswith(("pk_", "fk_", "uq_", "ck_")))
    assert unconventional == []
