"""Per-consumer request limiting.

The counters live in this process's memory. With one replica that is exact; with N replicas
a consumer gets N times the configured limit, because each process counts only the requests
it handled. Exact limiting needs shared state or an ingress that does the counting.

The key is a token the caller supplies, so anyone can mint an unbounded number of them. Two
consequences are handled here: entries are evicted oldest-first at a hard cap, and expired
entries are swept at most once per window rather than on every call — a sweep per call is
O(n) under the lock, which turns a memory problem into a latency one.
"""

import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

#: Distinct consumers tracked at once. Past this the oldest window is dropped, so under key
#: rotation the limiter degrades towards not limiting rather than towards blocking on a lock.
MAX_TRACKED = 10_000


@dataclass
class _Window:
    started: float
    count: int = 0


@dataclass
class RateLimiter:
    """A fixed window per consumer. ``check`` returns the seconds to wait, or None."""

    limit: int
    window: float = 60.0
    max_tracked: int = MAX_TRACKED
    _windows: OrderedDict[str, _Window] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _swept_at: float | None = None

    def check(self, consumer: str, now: float | None = None) -> int | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._sweep(now)

            window = self._windows.get(consumer)
            if window is None or now - window.started >= self.window:
                self._windows.pop(consumer, None)
                self._windows[consumer] = _Window(started=now, count=1)
                while len(self._windows) > self.max_tracked:
                    self._windows.popitem(last=False)
                return None

            if window.count >= self.limit:
                # At least one second: a Retry-After of 0 invites an immediate retry.
                return max(1, math.ceil(window.started + self.window - now))

            window.count += 1
            return None

    def _sweep(self, now: float) -> None:
        """Drop expired windows, at most once per window. Caller holds the lock."""
        if self._swept_at is not None and now - self._swept_at < self.window:
            return
        self._swept_at = now
        for name in [n for n, w in self._windows.items() if now - w.started >= self.window]:
            del self._windows[name]
