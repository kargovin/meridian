"""The runtime config plane (RFC §9) — reads that never fail, writes that refuse a stale page."""

import datetime as dt
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from meridian.db import runtime_config
from meridian.db.models import RuntimeConfig
from meridian.db.runtime_config import POLL_INTERVAL_SECONDS
from meridian.db.sources import StaleWrite

pytestmark = pytest.mark.postgres


def _token(session: Session, key: str) -> dt.datetime:
    """The version token a freshly rendered page would carry."""
    session.expire_all()
    row = session.get(RuntimeConfig, key)
    assert row is not None
    return row.updated_at


def test_a_seeded_knob_reads_its_stored_value(app_session: Session) -> None:
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 300


def test_a_written_value_is_what_the_next_read_sees(app_session: Session) -> None:
    """AC2 at the storage layer: the cadence is data, not a constant."""
    runtime_config.set_int(
        app_session,
        POLL_INTERVAL_SECONDS,
        value=120,
        expected_updated_at=_token(app_session, POLL_INTERVAL_SECONDS.key),
    )
    app_session.commit()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 120


def test_a_value_outside_the_bounds_is_refused(app_session: Session) -> None:
    """The floor stops someone spending the FR-I3 politeness budget in one keystroke."""
    with pytest.raises(ValueError):
        runtime_config.set_int(
            app_session,
            POLL_INTERVAL_SECONDS,
            value=1,
            expected_updated_at=_token(app_session, POLL_INTERVAL_SECONDS.key),
        )


def test_a_write_against_a_stale_token_is_refused(app_session: Session) -> None:
    """A page rendered before an out-of-band change must not write its old value back."""
    stale = _token(app_session, POLL_INTERVAL_SECONDS.key)
    runtime_config.set_int(app_session, POLL_INTERVAL_SECONDS, value=600, expected_updated_at=stale)
    app_session.commit()

    with pytest.raises(StaleWrite):
        runtime_config.set_int(
            app_session, POLL_INTERVAL_SECONDS, value=900, expected_updated_at=stale
        )


def test_the_version_token_moves_on_a_write_made_outside_the_orm(app_session: Session) -> None:
    """Trigger-maintained, because the emergency path is a psql session."""
    before = _token(app_session, POLL_INTERVAL_SECONDS.key)
    app_session.execute(
        sa.update(RuntimeConfig)
        .where(RuntimeConfig.key == POLL_INTERVAL_SECONDS.key)
        .values(value="480")
    )
    app_session.commit()
    assert _token(app_session, POLL_INTERVAL_SECONDS.key) > before


def test_a_value_that_cannot_be_parsed_falls_back_rather_than_raising(
    app_session: Session,
) -> None:
    """The caller is the discovery heartbeat: a config plane that can stop ingestion is worse
    than one that ignores a bad value.
    """
    app_session.execute(
        sa.update(RuntimeConfig)
        .where(RuntimeConfig.key == POLL_INTERVAL_SECONDS.key)
        .values(value="every five minutes")
    )
    app_session.commit()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 300


def test_a_stored_value_outside_the_bounds_falls_back_too(app_session: Session) -> None:
    """Bounds are enforced on write, so reaching this needs a hand-written UPDATE — which is
    exactly the path that bypasses the form.
    """
    app_session.execute(
        sa.update(RuntimeConfig)
        .where(RuntimeConfig.key == POLL_INTERVAL_SECONDS.key)
        .values(value="0")
    )
    app_session.commit()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 300


def test_a_missing_row_reads_the_default_and_cannot_be_written(app_session: Session) -> None:
    app_session.query(RuntimeConfig).delete()
    app_session.commit()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 300
    assert (
        runtime_config.set_int(
            app_session,
            POLL_INTERVAL_SECONDS,
            value=120,
            expected_updated_at=dt.datetime.now(dt.UTC),
        )
        is None
    )


def _seeded_by_migrations() -> dict[str, str]:
    """Every ``runtime_config`` row the migration tree inserts, as key -> value.

    Read out of the migration source rather than out of a database, deliberately. The test
    fixture re-creates a row for every declared knob after truncating, so a database read would
    find rows the fixture put there and report the invariant as held whether the migrations
    provide it or not.
    """
    seeded: dict[str, str] = {}
    for migration in Path("migrations/versions").glob("*.py"):
        text = migration.read_text()
        if "INSERT INTO runtime_config" not in text:
            continue
        for key, value in re.findall(r"VALUES\s*\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)", text):
            assert key not in seeded, f"{key} is seeded by more than one migration"
            seeded[key] = value
    return seeded


@pytest.mark.parametrize("knob", runtime_config.KNOBS, ids=lambda k: k.key)
def test_every_declared_knob_is_seeded_by_a_migration(knob: runtime_config.IntKnob) -> None:
    """⚠️ Parametrized over the declared knobs, not written against one by name.

    A knob with no seeded row still reads its default, so nothing breaks loudly — it simply
    cannot be edited through the admin surface, and the page says "no row" to whoever finds it.
    The earlier version of this test named ``poll_interval_seconds``, so the second knob added
    would have been unguarded.
    """
    assert knob.key in _seeded_by_migrations(), (
        f"{knob.key} is declared but no migration inserts its row"
    )


@pytest.mark.parametrize("knob", runtime_config.KNOBS, ids=lambda k: k.key)
def test_every_seeded_value_matches_its_declared_default(knob: runtime_config.IntKnob) -> None:
    """⚠️ A migration must not import application code, so the number is written twice and
    nothing but this holds the two together. Drift is silent: a fresh database would boot on one
    cadence while the code documents another.
    """
    assert int(_seeded_by_migrations()[knob.key]) == knob.default


def test_a_seeded_value_is_inside_the_knobs_own_bounds() -> None:
    """A seeded value outside the bounds would be rejected on read, so a fresh database would
    silently run on the fallback rather than on what the migration wrote.
    """
    for knob in runtime_config.KNOBS:
        knob.check(int(_seeded_by_migrations()[knob.key]))
