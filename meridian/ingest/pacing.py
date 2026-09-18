"""Spacing requests to one publisher (FR-I3).

``rate_limit_per_min`` is a promise about one host. It lives on the publisher rather than the
feed because N feeds each honouring it independently would exceed it N-fold — and for the same
reason there is one ``Pacer`` per process, shared by every job that talks to publishers.
Discovery and acquire run as two jobs on one scheduler's thread pool and can reach the same
host in the same second; two pacers would each keep the promise alone and break it together.

⚠️ The scope is the process, and that is load-bearing. A second process talking to publishers
has a second pacer and doubles the rate. Nothing here enforces "one process"; the deployment
does (T10, a single replica). If that changes, the reservation below moves into the database.
"""

import threading
import time
from collections.abc import Callable

from meridian.db.models import Source


class Pacer:
    """Hands out the next slot for a publisher, spaced by its rate and any ``Crawl-delay``.

    Slots are *reserved* under the lock and slept for outside it. Holding the lock while
    sleeping would make a 12 s wait for one publisher stall every other publisher's request.
    Reserving first means a thread that sleeps and then does nothing has still spent the
    slot — the conservative direction.
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        #: When each publisher's most recently reserved slot falls, on the monotonic clock.
        self._slot: dict[int, float] = {}

    @staticmethod
    def gap(source: Source, *, floor: float | None = None) -> float:
        """Seconds between two requests to this publisher: its rate, or the site's own
        ``Crawl-delay`` when that asks for more."""
        return max(60.0 / source.rate_limit_per_min, floor or 0.0)

    def wait_for(self, source: Source, *, floor: float | None = None) -> float:
        """How long the next request to this publisher would wait right now. Reserves nothing.

        For a caller that must decide whether to wait at all — a stage holding a lease cannot
        sleep longer than the lease and stay honest, so it asks first and releases the row
        instead when the answer is too long.
        """
        with self._lock:
            return self._wait(source, floor)

    def acquire(self, source: Source, *, floor: float | None = None) -> float:
        """Take the publisher's next slot, sleeping until it opens. Returns the time waited."""
        with self._lock:
            wait = self._wait(source, floor)
            self._slot[source.source_id] = self._clock() + wait
        if wait > 0:
            self._sleep(wait)
        return wait

    def _wait(self, source: Source, floor: float | None) -> float:
        previous = self._slot.get(source.source_id)
        if previous is None:
            return 0.0
        return max(previous + self.gap(source, floor=floor) - self._clock(), 0.0)
