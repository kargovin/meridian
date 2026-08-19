"""``source.created_at`` / ``updated_at``, and the trigger that maintains the second.

These are operational staleness — has anyone touched this row, and when. They are explicitly
not a change history: the next unrelated edit overwrites ``updated_at``, so it cannot say when
a particular field changed. That gap is RFC §11, and it is the same question for all four
config-plane knobs rather than this table's alone.

The timestamps come from ``now()``, which is transaction start time and does not advance
inside one transaction — so every test here commits between the write it makes and the write
it measures, exactly as two separate requests would.
"""

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Session

from meridian.db import sources
from tests.factories import make_source


def _timestamps(session: Session, source_id: int) -> tuple[dt.datetime, dt.datetime]:
    created, updated = session.execute(
        sa.text("SELECT created_at, updated_at FROM source WHERE source_id = :i"),
        {"i": source_id},
    ).one()
    return created, updated


def test_both_timestamps_are_set_on_insert(app_session: Session) -> None:
    source = make_source(app_session)
    app_session.commit()

    created, updated = _timestamps(app_session, source.source_id)

    assert created is not None
    assert updated is not None


def test_an_update_moves_updated_at_and_leaves_created_at(app_session: Session) -> None:
    source = make_source(app_session)
    app_session.commit()
    created_before, updated_before = _timestamps(app_session, source.source_id)

    sources.set_enabled(
        app_session, source.source_id, value=False, expected_updated_at=updated_before
    )
    app_session.commit()

    created_after, updated_after = _timestamps(app_session, source.source_id)
    assert created_after == created_before
    assert updated_after > updated_before


def test_the_trigger_fires_for_a_write_that_bypasses_the_orm(app_session: Session) -> None:
    """The reason this is a trigger and not SQLAlchemy's ``onupdate``.

    ``onupdate`` fires for ORM writes only. The path this column exists to record is the
    emergency one — a Legal call at 2am — and that is as likely to be a psql session as a
    form submission. A column that goes stale on exactly the path it was added for is worse
    than no column, because it reads as authoritative.
    """
    source = make_source(app_session)
    app_session.commit()
    _, before = _timestamps(app_session, source.source_id)

    app_session.execute(
        sa.text("UPDATE source SET enabled = false WHERE source_id = :i"),
        {"i": source.source_id},
    )
    app_session.commit()

    _, after = _timestamps(app_session, source.source_id)
    assert after > before


def test_the_trigger_does_not_let_a_caller_set_updated_at(app_session: Session) -> None:
    """BEFORE UPDATE overwrites whatever was supplied, so the column cannot be back-dated.

    Not a security control — anyone who can write the table can drop the trigger. It keeps an
    honest caller from recording a time that is not the time of the write.
    """
    source = make_source(app_session)
    app_session.commit()

    app_session.execute(
        sa.text(
            "UPDATE source SET enabled = false, updated_at = '2000-01-01T00:00:00Z' "
            "WHERE source_id = :i"
        ),
        {"i": source.source_id},
    )
    app_session.commit()

    _, updated = _timestamps(app_session, source.source_id)
    assert updated.year > 2000
