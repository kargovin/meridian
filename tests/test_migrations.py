"""The migration and the models must describe the same database."""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from meridian_contract import AcquisitionTier, PipelineState, Stage
from sqlalchemy.orm import Session

from meridian.db.models import Base
from tests.factories import make_article, make_feed, make_source, make_work

pytestmark = pytest.mark.postgres

#: The revision below ``c3e1fa9f2e61`` — downgrading to it runs that revision's own
#: downgrade, whatever has since been added on top.
_BELOW_TIER_0 = "9f21c4a7e0bd"
#: The revision below ``d3cdd889525c``, the dedup stage.
_BELOW_DEDUP = "c3e1fa9f2e61"
#: The revision below ``5b8e1d07a4c2``, the dedup knobs.
_BELOW_DEDUP_KNOBS = "d3cdd889525c"


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

    ⚠️ The target is named, never ``-1``. A relative target is resolved against the current
    head, so every migration added on top silently re-points it at the newest revision — the
    test goes on passing its own assertions while exercising a migration nobody wrote it for.
    """
    source = make_source(app_session)
    feed = make_feed(app_session, source, acquisition_tier=AcquisitionTier.UNAVAILABLE)
    app_session.commit()

    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, _BELOW_TIER_0)
            tier = conn.execute(
                sa.text("SELECT acquisition_tier FROM feed WHERE feed_id = :id"),
                {"id": feed.feed_id},
            ).scalar_one()
            assert tier == AcquisitionTier.EXTRACTION.value
        finally:
            command.upgrade(app_alembic_config, "head")


def test_downgrading_past_dedup_rewrites_rather_than_refuses(
    app_session: Session, app_migrated: sa.Engine, app_alembic_config: Config
) -> None:
    """Revision d3cdd889525c down: an article at ``deduped`` becomes ``acquired``.

    Same shape as the tier-0 case and the same stakes: ADD CONSTRAINT validates every existing
    row, so restoring the narrower CHECK over a database holding one article at ``deduped``
    refuses the whole downgrade — and ``app_migrated`` runs ``downgrade base`` at the start of
    every session, so the refusal strands the test database rather than failing one test.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.DEDUPED)
    app_session.commit()

    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, _BELOW_DEDUP)
            state = conn.execute(
                sa.text("SELECT pipeline_state FROM canonical_record WHERE article_id = :id"),
                {"id": article.article_id},
            ).scalar_one()
            assert state == PipelineState.ACQUIRED.value
        finally:
            command.upgrade(app_alembic_config, "head")


def test_the_dedup_migration_moves_work_already_in_flight(
    app_session: Session, app_migrated: sa.Engine, app_alembic_config: Config
) -> None:
    """Revision d3cdd889525c: an article waiting for the stage that dedup displaced is moved.

    The stage was inserted ahead of ``classify``, so every article already sitting at
    ``acquired`` holds a work row naming the stage that used to follow it. Nothing re-derives
    the queue on boot — it is a durable table — so a migration that only widens the CHECKs
    leaves those rows naming a stage no handler will claim on their behalf, and the articles
    stop with no error and no dead-letter row. The reconciler would report them, but nothing
    runs it on a schedule.

    Down then up, because the downgrade is what reconstructs the pre-stage state: it puts the
    row back on ``classify``, which is exactly what the upgrade then has to find.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.ACQUIRED)
    make_work(app_session, stage=Stage.DEDUP, article=article)
    app_session.commit()

    stage_now = sa.text("SELECT stage FROM pipeline_work WHERE article_id = :id")
    state_now = sa.text("SELECT pipeline_state FROM canonical_record WHERE article_id = :id")
    ids = {"id": article.article_id}

    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, _BELOW_DEDUP)
            assert conn.execute(stage_now, ids).scalar_one() == Stage.CLASSIFY.value
            assert conn.execute(state_now, ids).scalar_one() == PipelineState.ACQUIRED.value

            command.upgrade(app_alembic_config, "head")
            assert conn.execute(stage_now, ids).scalar_one() == Stage.DEDUP.value
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


def test_downgrading_past_the_dedup_knobs_removes_their_rows_alone(
    app_session: Session, app_migrated: sa.Engine, app_alembic_config: Config
) -> None:
    """Revision 5b8e1d07a4c2 down: the two dedup rows go and every other knob stays.

    ``downgrade base`` drops the whole table, so the full round trip cannot see a downgrade
    that leaves these rows behind — or one that deletes too much.
    """
    with app_migrated.begin() as conn:
        app_alembic_config.attributes["connection"] = conn
        try:
            command.downgrade(app_alembic_config, _BELOW_DEDUP_KNOBS)
            keys = set(conn.execute(sa.text("SELECT key FROM runtime_config")).scalars())
            assert keys == {"poll_interval_seconds", "acquire_interval_seconds"}
        finally:
            command.upgrade(app_alembic_config, "head")
