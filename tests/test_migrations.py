"""The migration and the models must describe the same database."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from meridian.db.models import Base

pytestmark = pytest.mark.postgres


def test_models_and_migration_agree(migrated: sa.Engine, alembic_config: Config) -> None:
    """AC1. Autogenerate against a migrated database must find nothing to do.

    Without this a hand-edited migration drifts from the models and the next autogenerate
    silently emits someone else's half-finished change.
    """
    with migrated.begin() as conn:
        alembic_config.attributes["connection"] = conn
        command.check(alembic_config)


def test_downgrade_leaves_no_residue(migrated: sa.Engine, alembic_config: Config) -> None:
    """AC5. up -> down -> up, with nothing of ours left behind in between."""
    with migrated.begin() as conn:
        alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(alembic_config, "base")
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
            command.upgrade(alembic_config, "head")


def test_every_entity_has_a_table(migrated: sa.Engine) -> None:
    inspector = sa.inspect(migrated)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())


def test_constraints_follow_the_naming_convention(migrated: sa.Engine) -> None:
    """A server-assigned name cannot be dropped by name in a later migration."""
    with migrated.connect() as conn:
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
