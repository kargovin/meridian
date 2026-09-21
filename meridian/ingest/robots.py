"""Reading and honouring ``robots.txt`` (FR-I3, RFC 9309).

One cache per process, shared by every job that fetches from a publisher. Fetched through the
same ``Fetcher`` as everything else, so the size cap, the deadline and the publisher's own
User-Agent apply, and through the same ``Pacer``, so the request counts against the
publisher's budget like any other.

What a status means, per RFC 9309 §2.3.1: a 2xx is the file; a 4xx is *unavailable* and the
site is open; a 5xx or no answer at all is *unreachable* and the site is **closed** — served
from the last good copy if there is one, and otherwise disallowed entirely. Failing open on an
outage is the mistake the spike found in ScrapeFlow; a publisher whose server is struggling is
the one we should least be adding to.

Matching is ``protego``'s: ``*`` and ``$``, the query string included, longest rule wins.
The stdlib parser compares path prefixes and reads ``Disallow: /*?traffic_source=`` as matching
nothing — the exact rule one roster publisher writes against the links its own feed emits.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from protego import Protego

from meridian.db.models import Source
from meridian.ingest.fetch import DEFAULT_USER_AGENT, Fetcher
from meridian.ingest.pacing import Pacer

log = logging.getLogger(__name__)

#: RFC 9309 §2.4 says a crawler SHOULD NOT use a cached copy for more than 24 hours.
DEFAULT_TTL = 24 * 60 * 60.0
#: How long an unreachable host stays closed before we ask again. Short, because the answer
#: while it stands is "fetch nothing from this publisher".
DEFAULT_FAILURE_TTL = 10 * 60.0


@dataclass(frozen=True)
class RobotsVerdict:
    """One answer for one URL, from one read of the cache.

    ``allowed`` is what the rules say — or False when the file could not be read and nothing
    earlier is held (RFC 9309 closes the host). ``outage_retry_in`` is set only in that second
    case: the seconds until the cache asks again, so a caller can record an outage as one and
    come back when the cache will, rather than treating a host that was down for ten minutes
    as a publisher that said no. ``crawl_delay`` is the site's own minimum spacing for our
    User-Agent, if the file states one.

    One read, one verdict: asking ``allowed`` and then ``outage`` as two calls can straddle the
    entry's expiry, and the second call fetches again — through the pacer, taking a slot — and
    may answer for a different file than the first.
    """

    allowed: bool
    outage_retry_in: float | None
    crawl_delay: float | None


@dataclass(frozen=True)
class _Entry:
    #: The parsed file; None when the site is open (4xx) or has never been read.
    rules: Protego | None
    #: True while the last fetch failed: the site is closed unless ``rules`` holds an older copy.
    unreachable: bool
    expires_at: float


class RobotsCache:
    def __init__(
        self,
        fetcher: Fetcher,
        pacer: Pacer,
        *,
        ttl: float = DEFAULT_TTL,
        failure_ttl: float = DEFAULT_FAILURE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetcher = fetcher
        self._pacer = pacer
        self._ttl = ttl
        self._failure_ttl = failure_ttl
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        # One lock per origin, so two jobs asking about one host share a single fetch while a
        # slow host's fetch stalls nobody else's lookup.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def check(self, url: str, source: Source) -> RobotsVerdict:
        """May this URL be fetched on behalf of this publisher's User-Agent — and if not, is
        that a rule or an outage. One cache read."""
        entry = self._entry(url, source)
        if entry.rules is None:
            if entry.unreachable:
                retry_in = max(entry.expires_at - self._clock(), 0.0)
                return RobotsVerdict(allowed=False, outage_retry_in=retry_in, crawl_delay=None)
            return RobotsVerdict(allowed=True, outage_retry_in=None, crawl_delay=None)
        agent = _agent(source)
        delay = entry.rules.crawl_delay(agent)
        return RobotsVerdict(
            allowed=bool(entry.rules.can_fetch(url, agent)),
            outage_retry_in=None,
            crawl_delay=float(delay) if delay is not None else None,
        )

    def allowed(self, url: str, source: Source) -> bool:
        """``check(...).allowed``. A caller that also needs to know *why* uses ``check``."""
        return self.check(url, source).allowed

    def crawl_delay(self, url: str, source: Source) -> float | None:
        """``check(...).crawl_delay``."""
        return self.check(url, source).crawl_delay

    def _entry(self, url: str, source: Source) -> _Entry:
        origin = _origin(url)
        with self._origin_lock(origin):
            entry = self._entries.get(origin)
            if entry is not None and self._clock() < entry.expires_at:
                return entry
            entry = self._fetch(origin, source, previous=entry)
            self._entries[origin] = entry
            return entry

    def _fetch(self, origin: str, source: Source, *, previous: _Entry | None) -> _Entry:
        self._pacer.acquire(source)
        result = self._fetcher(f"{origin}/robots.txt", user_agent=_agent(source), headers={})
        now = self._clock()

        if result.status is not None and 200 <= result.status < 300 and result.body is not None:
            rules = Protego.parse(result.body.decode("utf-8", errors="replace"))
            return _Entry(rules=rules, unreachable=False, expires_at=now + self._ttl)
        if result.status is not None and 400 <= result.status < 500:
            return _Entry(rules=None, unreachable=False, expires_at=now + self._ttl)

        # 5xx, 3xx that went nowhere, or no response: unreachable. Keep whatever good copy we
        # held — it is what RFC 9309 says to use while the host is down — and ask again soon.
        kept = previous.rules if previous is not None else None
        log.warning(
            "robots.txt at %s unreachable (status=%s error=%s): %s",
            origin,
            result.status,
            result.error,
            "using the cached copy" if kept is not None else "fetching nothing from this host",
        )
        return _Entry(rules=kept, unreachable=True, expires_at=now + self._failure_ttl)

    def _origin_lock(self, origin: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(origin)
            if lock is None:
                lock = self._locks[origin] = threading.Lock()
            return lock


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _agent(source: Source) -> str:
    return source.user_agent or DEFAULT_USER_AGENT
