"""The discovery heartbeat (RFC §6.3, T6).

APScheduler in-process. Three timed things exist in this system, which is a scheduler rather
than a workflow engine.

⚠️ The cadence is re-read after every cycle and the job re-scheduled from it. The obvious
construction — an interval trigger built once at startup — captures the value at boot, so
changing it in the config plane does nothing until someone restarts the process, and nothing
anywhere reports that. Freshness is the one budget where a silent regression is expected to go
unnoticed (§7.1: loosening the cadence is the cheapest way to lose freshness, with no code
change and no diff to review), so the read has to happen on the clock, not at import.
"""

import datetime as dt
import logging
from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from meridian.db import runtime_config
from meridian.ingest.discovery import CycleReport, run_cycle
from meridian.ingest.fetch import Fetcher

log = logging.getLogger(__name__)

JOB_ID = "discovery-poll"


def poll_interval_seconds(session: Session) -> int:
    """The configured cadence. Falls back to the declared default rather than raising."""
    return runtime_config.get_int(session, runtime_config.POLL_INTERVAL_SECONDS)


class DiscoveryScheduler:
    """Runs ``run_cycle`` on the configured cadence, re-reading it every time.

    ``max_instances=1`` and ``coalesce=True`` together mean a cycle that overruns its interval
    delays the next one rather than running two pollers at once — which would double every
    request against every publisher, breaking the FR-I3 promise in the one situation where we
    are already struggling.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        fetcher: Fetcher,
        *,
        scheduler: BackgroundScheduler | None = None,
        run: Callable[[Session, Fetcher], CycleReport] = run_cycle,
    ) -> None:
        self._sessions = sessions
        self._fetcher = fetcher
        self._scheduler = scheduler or BackgroundScheduler()
        self._run = run
        self._interval: int | None = None

    def start(self) -> None:
        with self._sessions() as session:
            interval = poll_interval_seconds(session)
        self._interval = interval
        self._scheduler.add_job(
            self.tick,
            trigger=IntervalTrigger(seconds=interval),
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            # ⚠️ Without this the first poll happens one whole interval after start, so every
            # deploy costs up to an interval of freshness and a crash-looping process never
            # polls at all. The trigger's own first fire is interval-from-now, not now.
            next_run_time=dt.datetime.now(dt.UTC),
        )
        self._scheduler.start()
        log.info("discovery poll started at %d s", interval)

    def tick(self) -> CycleReport:
        """One cycle, then re-read the cadence.

        ⚠️ The re-read is in a ``finally``: a cycle that raises must still leave the heartbeat
        scheduled, or one bad poll stops discovery permanently and the symptom is silence.
        """
        report = CycleReport()
        try:
            with self._sessions() as session:
                report = self._run(session, self._fetcher)
                log.info(
                    "discovery cycle: polled=%d unchanged=%d failed=%d discovered=%d "
                    "skipped=%d in %.1fs",
                    report.polled,
                    report.not_modified,
                    report.failed,
                    report.discovered,
                    report.skipped_feeds,
                    report.duration_seconds,
                )
                self._warn_if_overrunning(report)
        finally:
            self._apply_cadence()
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

    def _apply_cadence(self) -> None:
        with self._sessions() as session:
            interval = poll_interval_seconds(session)
        if interval == self._interval:
            return
        log.info("discovery poll cadence changed %s s -> %d s", self._interval, interval)
        self._interval = interval
        self._scheduler.reschedule_job(JOB_ID, trigger=IntervalTrigger(seconds=interval))

    def shutdown(self) -> None:
        self._scheduler.shutdown()
