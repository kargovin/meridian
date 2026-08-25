"""The discovery heartbeat's cadence (AC2, RFC §6.3/§9).

The scheduler itself is stubbed. What is under test is that the cadence is re-read from the
config plane rather than captured once at startup — the failure mode being silent.
"""

from collections.abc import Callable, Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from meridian.db import runtime_config
from meridian.db.models import RuntimeConfig
from meridian.db.runtime_config import POLL_INTERVAL_SECONDS
from meridian.db.session import session_factory
from meridian.ingest.discovery import CycleReport
from meridian.ingest.fetch import Fetcher, FetchResult
from meridian.ingest.scheduler import JOB_ID, DiscoveryScheduler

pytestmark = pytest.mark.postgres


class StubScheduler:
    def __init__(self) -> None:
        self.added: list[tuple[str, float]] = []
        self.rescheduled: list[tuple[str, float]] = []
        self.started = False

    def add_job(self, func: Any, *, trigger: Any, id: str, **kw: Any) -> None:
        self.added.append((id, trigger.interval.total_seconds()))

    def reschedule_job(self, job_id: str, *, trigger: Any) -> None:
        self.rescheduled.append((job_id, trigger.interval.total_seconds()))

    def start(self) -> None:
        self.started = True

    def shutdown(self) -> None:
        self.started = False


def _set_interval_with(sessions: sessionmaker[Session], seconds: int) -> None:
    """Change the cadence the way an operator would: committed, through its own session."""
    with sessions() as session:
        row = session.get(RuntimeConfig, POLL_INTERVAL_SECONDS.key)
        assert row is not None
        runtime_config.set_int(
            session,
            POLL_INTERVAL_SECONDS,
            value=seconds,
            expected_updated_at=row.updated_at,
        )
        session.commit()


@pytest.fixture
def sessions(app_migrated: sa.Engine, app_session: Session) -> sessionmaker[Session]:
    """A real factory, so the scheduler reads through a session of its own.

    Depends on ``app_session`` for its truncate-and-reseed. Writes made by a test are committed
    before the scheduler reads, which is the same ordering a running process sees.
    """
    return session_factory(app_migrated)


def _fetcher(url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
    raise AssertionError("the cycle is stubbed; the fetcher must never be called")


def _scheduler(
    sessions: sessionmaker[Session],
    stub: StubScheduler,
    run: Callable[[Session, Fetcher], CycleReport] | None = None,
) -> DiscoveryScheduler:
    return DiscoveryScheduler(
        sessions,
        _fetcher,
        scheduler=stub,
        run=run or (lambda session, fetcher: CycleReport()),
    )


def test_the_job_starts_on_the_configured_cadence(sessions: sessionmaker[Session]) -> None:
    _set_interval_with(sessions, 120)
    stub = StubScheduler()

    _scheduler(sessions, stub).start()

    assert stub.added == [(JOB_ID, 120.0)]
    assert stub.started


def test_changing_the_cadence_takes_effect_without_a_restart(
    sessions: sessionmaker[Session],
) -> None:
    """AC2, and the whole point of the story's config requirement.

    ⚠️ The obvious construction — one interval trigger built at startup — passes every other
    test in this file and fails only this one, because it captures the value at boot. Nothing
    errors; the cadence simply never changes again.
    """
    stub = StubScheduler()
    scheduler = _scheduler(sessions, stub)
    scheduler.start()

    _set_interval_with(sessions, 900)
    scheduler.tick()

    assert stub.rescheduled == [(JOB_ID, 900.0)]


def test_an_unchanged_cadence_does_not_reschedule(sessions: sessionmaker[Session]) -> None:
    """Rescheduling restarts the interval from now, so doing it every cycle would stretch the
    effective gap by however long a cycle takes.
    """
    stub = StubScheduler()
    scheduler = _scheduler(sessions, stub)
    scheduler.start()

    scheduler.tick()

    assert stub.rescheduled == []


def test_a_cycle_that_raises_still_leaves_the_heartbeat_scheduled(
    sessions: sessionmaker[Session],
) -> None:
    """⚠️ Without the finally, one bad cycle stops discovery permanently and the symptom is
    silence — no error on any later tick, because there are no later ticks.
    """

    def explode(session: Session, fetcher: Any) -> CycleReport:
        raise RuntimeError("boom")

    stub = StubScheduler()
    scheduler = _scheduler(sessions, stub, run=explode)
    scheduler.start()
    _set_interval_with(sessions, 600)

    with pytest.raises(RuntimeError):
        scheduler.tick()

    assert stub.rescheduled == [(JOB_ID, 600.0)]
