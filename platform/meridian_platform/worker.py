"""The loops that call the one-unit-of-work functions repeatedly.

Deliberately thin. All the behaviour is in ``jobs.process_next`` and ``retention.sweep``,
which take a session and return, so they can be tested directly; what is left here is a
sleep and a stop flag.
"""

import datetime as dt
import logging
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from meridian_platform.jobs import now_utc, process_next
from meridian_platform.retention import sweep

log = logging.getLogger(__name__)

WORK_INTERVAL = 1.0
SWEEP_INTERVAL = 300.0


class BackgroundLoops:
    """Two daemon threads: one draining the queue, one enforcing retention."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        retention: dt.timedelta | None = None,
        work_interval: float = WORK_INTERVAL,
        sweep_interval: float = SWEEP_INTERVAL,
    ) -> None:
        self._session_factory = session_factory
        self._retention = retention
        self._work_interval = work_interval
        self._sweep_interval = sweep_interval
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for name, interval, step in (
            ("jobs", self._work_interval, self._work),
            ("retention", self._sweep_interval, self._sweep),
        ):
            thread = threading.Thread(
                target=self._loop, args=(interval, step), name=name, daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def _loop(self, interval: float, step: Callable[[Session], bool]) -> None:
        while not self._stopping.is_set():
            try:
                with self._session_factory() as session:
                    busy = step(session)
            except Exception:
                # A failing step must not kill the thread; the next tick retries.
                log.exception("background step failed")
                busy = False
            if not busy:
                self._stopping.wait(interval)

    def _work(self, session: Session) -> bool:
        return process_next(session, retention=self._retention)

    def _sweep(self, session: Session) -> bool:
        result = sweep(session, now_utc())
        if result.overdue:
            log.warning("retention is behind: %s rows past their window", result.overdue)
        return False
