"""Per-consumer request limiting.

The counters live in this process's memory. With one replica that is exact; with N replicas
a consumer gets N times the configured limit, because each process counts only the requests
it handled. Exact limiting needs shared state or an ingress that does the counting.
"""

import math
import threading
import time
from dataclasses import dataclass, field

_MAX_TRACKED = 10_000


@dataclass
class _Window:
    started: float
    count: int = 0


@dataclass
class RateLimiter:
    """A fixed window per consumer. ``check`` returns the seconds to wait, or None."""

    limit: int
    window: float = 60.0
    _windows: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, consumer: str, now: float | None = None) -> int | None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._windows) > _MAX_TRACKED:
                # The key is an unverified token, so anyone can mint new ones; without this
                # the map grows without bound. Expired windows carry no state worth keeping.
                self._windows = {
                    name: w for name, w in self._windows.items() if now - w.started < self.window
                }
            window = self._windows.get(consumer)
            if window is None or now - window.started >= self.window:
                self._windows[consumer] = _Window(started=now, count=1)
                return None

            if window.count >= self.limit:
                # At least one second: a Retry-After of 0 invites an immediate retry.
                return max(1, math.ceil(window.started + self.window - now))

            window.count += 1
            return None
