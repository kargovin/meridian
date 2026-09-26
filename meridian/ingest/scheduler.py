"""The pipeline's timed jobs (RFC §6.3, T6).

APScheduler in-process. A scheduler rather than a workflow engine: nothing here knows what
follows what — the stage sequence is data in ``libs/contract`` and each job simply claims its
own work.

⚠️ Every cadence is re-read after every run and the job rescheduled from it. The obvious
construction — an interval trigger built once at startup — captures the value at boot, so
changing it in the config plane does nothing until someone restarts the process, and nothing
anywhere reports that. Freshness is the one budget where a silent regression is expected to go
unnoticed (§7.1: loosening a cadence is the cheapest way to lose freshness, with no code change
and no diff to review), so the read has to happen on the clock, not at import.
"""

import datetime as dt
import logging
from collections.abc import Callable
from typing import Protocol

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from meridian.db import runtime_config
from meridian.db.runtime_config import IntKnob
from meridian.dedup import stage as dedup
from meridian.dedup.stage import DedupReport
from meridian.ingest.acquire import AcquireReport, run_batch
from meridian.ingest.discovery import CycleReport, run_cycle
from meridian.ingest.network import Network


class AcquireRun(Protocol):
    """What the acquire job calls. Spelled out so the default is type-checked like a stub."""

    def __call__(
        self,
        session: Session,
        *,
        network: Network,
        lease: dt.timedelta,
        limit: int = ...,
        worker: str | None = ...,
    ) -> AcquireReport: ...


class DedupRun(Protocol):
    """What the dedup job calls. Spelled out for the same reason as ``AcquireRun``."""

    def __call__(
        self,
        session: Session,
        *,
        lease: dt.timedelta,
        hamming_bits: int,
        limit: int = ...,
        worker: str | None = ...,
    ) -> DedupReport: ...


log = logging.getLogger(__name__)

JOB_ID = "discovery-poll"
ACQUIRE_JOB_ID = "acquire-batch"
DEDUP_JOB_ID = "dedup-batch"


def poll_interval_seconds(session: Session) -> int:
    """The configured discovery cadence. Falls back to the declared default rather than raising."""
    return runtime_config.get_int(session, runtime_config.POLL_INTERVAL_SECONDS)


class _CadencedJob[ReportT]:
    """A job whose interval is a runtime knob, re-read after every run.

    ``max_instances=1`` and ``coalesce=True`` together mean a run that overruns its interval
    delays the next one rather than running two at once — which for discovery would double
    every request against every publisher, breaking the FR-I3 promise in the one situation
    where we are already struggling.
    """

    job_id: str
    knob: IntKnob

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        scheduler: BackgroundScheduler | None = None,
    ) -> None:
        self._sessions = sessions
        self._scheduler = scheduler or BackgroundScheduler()
        self._interval: int | None = None

    def _interval_seconds(self, session: Session) -> int:
        return runtime_config.get_int(session, self.knob)

    def _run_once(self, session: Session) -> ReportT:
        raise NotImplementedError

    def start(self) -> None:
        with self._sessions() as session:
            interval = self._interval_seconds(session)
        self._interval = interval
        self._scheduler.add_job(
            self.tick,
            trigger=IntervalTrigger(seconds=interval),
            id=self.job_id,
            max_instances=1,
            coalesce=True,
            # ⚠️ Without this the first run happens one whole interval after start, so every
            # deploy costs up to an interval of freshness and a crash-looping process never
            # runs at all. The trigger's own first fire is interval-from-now, not now.
            next_run_time=dt.datetime.now(dt.UTC),
        )
        self._start_scheduler()
        log.info("%s started at %d s", self.job_id, interval)

    def _start_scheduler(self) -> None:
        """Start the underlying scheduler unless something else already has.

        Two jobs share one scheduler in a running process, and starting an already-running
        APScheduler raises.
        """
        if not self._scheduler.running:
            self._scheduler.start()

    def tick(self) -> ReportT | None:
        """One run, then re-read the cadence.

        ⚠️ The re-read is in a ``finally``: a run that raises must still leave the job
        scheduled, or one bad run stops the stage permanently and the symptom is silence.
        """
        try:
            with self._sessions() as session:
                return self._run_once(session)
        finally:
            self._apply_cadence()

    def _apply_cadence(self) -> None:
        with self._sessions() as session:
            interval = self._interval_seconds(session)
        if interval == self._interval:
            return
        log.info("%s cadence changed %s s -> %d s", self.job_id, self._interval, interval)
        self._interval = interval
        self._scheduler.reschedule_job(self.job_id, trigger=IntervalTrigger(seconds=interval))

    def shutdown(self) -> None:
        self._scheduler.shutdown()


class DiscoveryScheduler(_CadencedJob[CycleReport]):
    """Runs ``run_cycle`` on the configured cadence, re-reading it every time."""

    job_id = JOB_ID
    knob = runtime_config.POLL_INTERVAL_SECONDS

    def __init__(
        self,
        sessions: sessionmaker[Session],
        network: Network,
        *,
        scheduler: BackgroundScheduler | None = None,
        run: Callable[[Session, Network], CycleReport] = run_cycle,
    ) -> None:
        super().__init__(sessions, scheduler=scheduler)
        self._network = network
        self._run = run

    def _run_once(self, session: Session) -> CycleReport:
        report = self._run(session, self._network)
        log.info(
            "discovery cycle: polled=%d unchanged=%d failed=%d discovered=%d skipped=%d "
            "robots_blocked=%d in %.1fs",
            report.polled,
            report.not_modified,
            report.failed,
            report.discovered,
            report.skipped_feeds,
            report.robots_blocked,
            report.duration_seconds,
        )
        self._warn_if_overrunning(report)
        return report

    def _warn_if_overrunning(self, report: CycleReport) -> None:
        """Say so when a cycle takes longer than the gap it is supposed to fit inside.

        ⚠️ This degrades silently otherwise. ``coalesce`` and ``max_instances=1`` correctly stop
        an overrunning cycle from doubling requests against every publisher, but the cost is
        that the effective cadence quietly *becomes* the cycle time — the configured interval
        stops being what happens, and nothing we own says so. The cycle also sits inside the
        freshness budget rather than beside it: an article waits the interval plus its feed's
        position in the cycle (RFC §7.1).

        An instrument, not a defence — following §6.3's pattern. It reports; it does not throttle.
        """
        if self._interval is None or report.duration_seconds <= self._interval:
            return
        log.warning(
            "discovery cycle took %.1fs against a %ds interval: the effective cadence is now "
            "the cycle time, not the configured one. Freshness is worse than configured. "
            "Reduce feeds, raise rate_limit_per_min, or widen the interval deliberately.",
            report.duration_seconds,
            self._interval,
        )


class AcquireScheduler(_CadencedJob[AcquireReport]):
    """Runs the acquire stage on the configured cadence.

    A job of its own rather than a step at the end of a discovery cycle. They are separate
    because acquire is about to grow a network fetch of its own, and running it inside the
    poll would let one slow article delay discovery for every publisher.
    """

    job_id = ACQUIRE_JOB_ID
    knob = runtime_config.ACQUIRE_INTERVAL_SECONDS

    def __init__(
        self,
        sessions: sessionmaker[Session],
        network: Network,
        *,
        lease: dt.timedelta,
        limit: int = 50,
        scheduler: BackgroundScheduler | None = None,
        # ⚠️ A precise signature, not ``Callable[...]``. The ellipsis form disables argument
        # checking entirely: renaming a keyword here left 24 tests passing and mypy clean
        # while the job died on its first tick, because every test injects a stub and
        # nothing exercises the default.
        run: AcquireRun = run_batch,
    ) -> None:
        super().__init__(sessions, scheduler=scheduler)
        self._network = network
        self._lease = lease
        self._limit = limit
        self._run = run

    def _run_once(self, session: Session) -> AcquireReport:
        report = self._run(session, network=self._network, lease=self._lease, limit=self._limit)
        # Logged only when there was something to do: this runs every 30 s against a roster
        # that is usually quiet, and a line per empty batch buries the ones that matter.
        if report.claimed:
            log.info(
                "acquire batch: claimed=%d acquired=%d dropped=%d failed=%d stale=%d "
                "fetched=%d robots_blocked=%d refused=%d deferred=%d abandoned=%d "
                "extract_empty=%d adapter_missing=%d withheld=%d",
                report.claimed,
                report.acquired,
                report.dropped,
                report.failed,
                report.stale,
                report.fetched,
                report.robots_blocked,
                report.fetch_refused,
                report.fetch_deferred,
                report.fetch_abandoned,
                report.extract_empty,
                report.adapter_missing,
                report.withheld,
            )
        return report


class DedupScheduler(_CadencedJob[DedupReport]):
    """Runs the dedup stage on the configured cadence.

    Makes no request to any publisher, so it takes no ``Network``: the comparison is a query
    against records already held.

    ⚠️ The match threshold is read from the config plane at the start of every batch, not once
    at startup. It is a knob precisely so it can be tuned against what dedup is collapsing, and
    a value captured at boot would make every tuning step a restart — or, worse, look applied on
    the admin page while the running process kept the old one.
    """

    job_id = DEDUP_JOB_ID
    knob = runtime_config.DEDUP_INTERVAL_SECONDS

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        lease: dt.timedelta,
        limit: int = 50,
        scheduler: BackgroundScheduler | None = None,
        run: DedupRun = dedup.run_batch,
    ) -> None:
        super().__init__(sessions, scheduler=scheduler)
        self._lease = lease
        self._limit = limit
        self._run = run

    def _run_once(self, session: Session) -> DedupReport:
        hamming_bits = runtime_config.get_int(session, runtime_config.DEDUP_HAMMING_BITS)
        report = self._run(session, lease=self._lease, hamming_bits=hamming_bits, limit=self._limit)
        # Logged only when there was something to do, as acquire is.
        if report.claimed:
            log.info(
                "dedup batch at %d bits: claimed=%d advanced=%d collapsed=%d (exact=%d near=%d) "
                "already_noted=%d no_body=%d same_publisher=%d failed=%d dead_lettered=%d stale=%d",
                hamming_bits,
                report.claimed,
                report.advanced,
                report.collapsed,
                report.exact,
                report.near,
                report.already_noted,
                report.no_body,
                report.same_publisher,
                report.failed,
                report.dead_lettered,
                report.stale,
            )
        return report
