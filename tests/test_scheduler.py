"""The discovery heartbeat's cadence (AC2, RFC §6.3/§9).

The scheduler itself is stubbed. What is under test is that the cadence is re-read from the
config plane rather than captured once at startup — the failure mode being silent.
"""

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from meridian.db import runtime_config
from meridian.db.models import RuntimeConfig
from meridian.db.runtime_config import ACQUIRE_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS
from meridian.db.session import session_factory
from meridian.ingest.acquire import AcquireReport
from meridian.ingest.discovery import CycleReport
from meridian.ingest.fetch import Fetcher, FetchResult
from meridian.ingest.scheduler import (
    ACQUIRE_JOB_ID,
    JOB_ID,
    AcquireScheduler,
    DiscoveryScheduler,
)

pytestmark = pytest.mark.postgres


class StubScheduler:
    def __init__(self) -> None:
        self.added: list[tuple[str, float]] = []
        self.rescheduled: list[tuple[str, float]] = []
        self.started = False
        self.first_run_at: dt.datetime | None = None

    def add_job(self, func: Any, *, trigger: Any, id: str, **kw: Any) -> None:
        self.added.append((id, trigger.interval.total_seconds()))
        self.first_run_at = kw.get("next_run_time")

    def reschedule_job(self, job_id: str, *, trigger: Any) -> None:
        self.rescheduled.append((job_id, trigger.interval.total_seconds()))

    def start(self) -> None:
        # ⚠️ Raises, because APScheduler raises SchedulerAlreadyRunningError. A stub that
        # merely re-set a flag here would let the double-start test pass with the guard in
        # `_start_scheduler` deleted — asserting nothing about the behaviour it is named for.
        if self.started:
            raise RuntimeError("scheduler is already running")
        self.started = True

    def shutdown(self) -> None:
        self.started = False

    @property
    def running(self) -> bool:
        """Two jobs share one scheduler in a real process, so the second to start must not
        try to start it again.
        """
        return self.started


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


def test_the_first_poll_does_not_wait_a_whole_interval(
    sessions: sessionmaker[Session],
) -> None:
    """⚠️ An interval trigger's first fire is one interval from now, not now. Without an explicit
    first run every deploy costs up to an interval of freshness, and a crash-looping process
    never polls at all.
    """
    stub = StubScheduler()

    _scheduler(sessions, stub).start()

    assert stub.first_run_at is not None
    assert (dt.datetime.now(dt.UTC) - stub.first_run_at).total_seconds() < 5


def test_a_cycle_that_outruns_its_interval_says_so(
    sessions: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ This is the silent one. coalesce/max_instances correctly stop an overrunning cycle
    doubling requests against publishers — and the cost is that the effective cadence quietly
    becomes the cycle time. Nothing we own would otherwise say the configured interval has
    stopped being what happens.
    """
    stub = StubScheduler()
    slow = DiscoveryScheduler(
        sessions,
        _fetcher,
        scheduler=stub,
        run=lambda session, fetcher: CycleReport(polled=24, duration_seconds=400.0),
    )
    slow.start()

    with caplog.at_level("WARNING"):
        slow.tick()

    assert any("effective cadence" in record.getMessage() for record in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_a_cycle_inside_its_interval_is_not_reported(
    sessions: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    stub = StubScheduler()
    fast = DiscoveryScheduler(
        sessions,
        _fetcher,
        scheduler=stub,
        run=lambda session, fetcher: CycleReport(polled=24, duration_seconds=30.0),
    )
    fast.start()

    with caplog.at_level("WARNING"):
        fast.tick()

    assert not [r for r in caplog.records if "effective cadence" in r.getMessage()]


# --------------------------------------------------------------------------- the acquire job


def _set_knob(sessions: sessionmaker[Session], knob: runtime_config.IntKnob, value: int) -> None:
    with sessions() as session:
        row = session.get(RuntimeConfig, knob.key)
        assert row is not None
        runtime_config.set_int(session, knob, value=value, expected_updated_at=row.updated_at)
        session.commit()


def _acquire(sessions: sessionmaker[Session], stub: StubScheduler, **kw: Any) -> AcquireScheduler:
    return AcquireScheduler(
        sessions,
        lease=dt.timedelta(minutes=5),
        scheduler=stub,
        run=kw.pop("run", lambda session, **_: AcquireReport()),
        **kw,
    )


def test_the_acquire_job_reads_its_own_cadence(sessions: sessionmaker[Session]) -> None:
    """Its own knob, not the poll interval — the two are different terms in the same budget
    and tying them together would mean one cannot be tuned without moving the other.
    """
    _set_knob(sessions, ACQUIRE_INTERVAL_SECONDS, 45)
    stub = StubScheduler()

    _acquire(sessions, stub).start()

    assert stub.added == [(ACQUIRE_JOB_ID, 45.0)]


def test_changing_the_acquire_cadence_takes_effect_without_a_restart(
    sessions: sessionmaker[Session],
) -> None:
    stub = StubScheduler()
    scheduler = _acquire(sessions, stub)
    scheduler.start()

    _set_knob(sessions, ACQUIRE_INTERVAL_SECONDS, 120)
    scheduler.tick()

    assert stub.rescheduled == [(ACQUIRE_JOB_ID, 120.0)]


def test_a_batch_that_raises_still_leaves_the_job_scheduled(
    sessions: sessionmaker[Session],
) -> None:
    """⚠️ Without the finally, one bad batch stops the stage permanently and the symptom is a
    queue that quietly stops draining.
    """

    def explode(session: Session, **_: Any) -> AcquireReport:
        raise RuntimeError("boom")

    stub = StubScheduler()
    scheduler = _acquire(sessions, stub, run=explode)
    scheduler.start()

    _set_knob(sessions, ACQUIRE_INTERVAL_SECONDS, 90)
    with pytest.raises(RuntimeError):
        scheduler.tick()

    assert stub.rescheduled == [(ACQUIRE_JOB_ID, 90.0)]


def test_two_jobs_share_one_scheduler_and_start_it_once(
    sessions: sessionmaker[Session],
) -> None:
    """⚠️ APScheduler raises on start() when it is already running, so the second job to start
    would take the process down at boot — after the first had been scheduled, which is the
    kind of half-started state that reads as a crash loop with no obvious cause.
    """
    stub = StubScheduler()
    discovery = _scheduler(sessions, stub)
    acquire = _acquire(sessions, stub)

    discovery.start()
    acquire.start()

    assert [job_id for job_id, _ in stub.added] == [JOB_ID, ACQUIRE_JOB_ID]
    assert stub.started
