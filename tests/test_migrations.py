"""The migration and the models must describe the same database."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from meridian_contract import AcquisitionTier
from sqlalchemy.orm import Session

from meridian.db.models import Base
from tests.factories import make_feed, make_source

pytestmark = pytest.mark.postgres


def test_models_and_migration_agree(app_migrated: sa.Engine, app_alembic_config: Config) -> None:
    """AC1. Autogenerate against a migrated database must find nothing to do.

    Without this a hand-edited migration drifts from the models and the next autogenerate
    silently emits someone else's half-finished change.
    """
    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        command.check(app_alembic_config)


def test_downgrade_leaves_no_residue(app_migrated: sa.Engine, app_alembic_config: Config) -> None:
    """AC5. up -> down -> up, with nothing of ours left behind in between."""
    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, "base")
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
            command.upgrade(app_alembic_config, "head")


def test_downgrading_past_tier_0_rewrites_rather_than_refuses(
    app_session: Session, app_migrated: sa.Engine, app_alembic_config: Config
) -> None:
    """Revision c3e1fa9f2e61 down: a feed at ``0_unavailable`` becomes ``3_extraction``.

    ADD CONSTRAINT validates existing rows, so without the rewrite the downgrade refuses on
    any database holding a tier-0 feed — and ``app_migrated`` runs ``downgrade base`` on
    whatever the previous test run left behind, which would strand the test database.
    """
    source = make_source(app_session)
    feed = make_feed(app_session, source, acquisition_tier=AcquisitionTier.UNAVAILABLE)
    app_session.commit()

    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, "-1")
            tier = conn.execute(
                sa.text("SELECT acquisition_tier FROM feed WHERE feed_id = :id"),
                {"id": feed.feed_id},
            ).scalar_one()
            assert tier == AcquisitionTier.EXTRACTION.value
        finally:
            command.upgrade(app_alembic_config, "head")


def test_every_entity_has_a_table(app_migrated: sa.Engine) -> None:
    inspector = sa.inspect(app_migrated)
    assert set(Base.metadata.tables) <= set(inspector.get_table_names())


def test_constraints_follow_the_naming_convention(app_migrated: sa.Engine) -> None:
    """A server-assigned name cannot be dropped by name in a later migration."""
    with app_migrated.connect() as conn:
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
