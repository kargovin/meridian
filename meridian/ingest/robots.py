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

    def allowed(self, url: str, source: Source) -> bool:
        """May this URL be fetched on behalf of this publisher's User-Agent."""
        entry = self._entry(url, source)
        if entry.rules is None:
            return not entry.unreachable
        return bool(entry.rules.can_fetch(url, _agent(source)))

    def closed_by_outage(self, url: str, source: Source) -> float | None:
        """Seconds until this origin's ``robots.txt`` is asked for again, when it could not be
        read and no earlier copy is held — the case ``allowed`` answers False for without any
        rule having been written. None when a rule (or the absence of one) is what answers.

        A caller can then tell an outage from a refusal: record it as one, and come back when
        the cache will, rather than treating a host that was down for ten minutes as a
        publisher that said no.
        """
        entry = self._entry(url, source)
        if entry.rules is not None or not entry.unreachable:
            return None
        return max(entry.expires_at - self._clock(), 0.0)

    def crawl_delay(self, url: str, source: Source) -> float | None:
        """The site's own minimum spacing for our User-Agent, if it states one."""
        entry = self._entry(url, source)
        if entry.rules is None:
            return None
        delay = entry.rules.crawl_delay(_agent(source))
        return float(delay) if delay is not None else None

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
