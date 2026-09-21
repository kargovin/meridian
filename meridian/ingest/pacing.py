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
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable

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

    def try_acquire(
        self, source: Source, *, max_wait: float, floor: float | None = None
    ) -> float | None:
        """Take the next slot if it opens within ``max_wait`` seconds; otherwise reserve nothing.

        Returns the time waited, or None when the slot was too far off. One lock for the
        decision and the reservation together: ``wait_for`` followed by ``acquire`` leaves a
        gap in which another job can take the slot, and the caller then sleeps past the bound
        it just checked against — a lease-holding stage would overstay its lease by exactly
        the amount it asked about.
        """
        with self._lock:
            wait = self._wait(source, floor)
            if wait > max_wait:
                return None
            self._slot[source.source_id] = self._clock() + wait
        if wait > 0:
            self._sleep(wait)
        return wait

    def _wait(self, source: Source, floor: float | None) -> float:
        previous = self._slot.get(source.source_id)
        if previous is None:
            return 0.0
        return max(previous + self.gap(source, floor=floor) - self._clock(), 0.0)


def round_robin[T](items: Iterable[T], *, key: Callable[[T], Hashable]) -> list[T]:
    """Order items so consecutive ones have different keys, keeping each key's own order.

    For a batch of requests grouped by publisher. Politeness is per publisher, so issuing one
    publisher's requests back to back means waiting out the full gap before each — the worst
    possible order, and the one a database's natural order tends to produce. Round-robin
    instead: by the time the batch returns to a publisher it has spent the other publishers'
    requests, and that time counts towards the gap.

    Measured on discovery, 8 publishers x 3 feeds at 5 requests/min: 194 s in feed order, 26 s
    interleaved. Identical politeness — every publisher still sees the same minimum spacing —
    for a seventh of the wall time.
    """
    queues: dict[Hashable, list[T]] = defaultdict(list)
    for item in items:
        queues[key(item)].append(item)

    ordered: list[T] = []
    pending = list(queues.values())
    while pending:
        for queue in pending:
            ordered.append(queue.pop(0))
        pending = [queue for queue in pending if queue]
    return ordered
