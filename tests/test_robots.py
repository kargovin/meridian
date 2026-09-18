"""``robots.txt``: matching, caching, and what a failure means. No database."""

import threading
from collections.abc import Mapping

from meridian_contract import RightsLevel

from meridian.db.models import Source
from meridian.ingest.fetch import FetchResult
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache

AL_JAZEERA = b"""User-agent: *
Disallow: /*?traffic_source=
Crawl-delay: 7
"""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Fetcher:
    """Answers per URL; the answers can be changed between calls."""

    def __init__(self, answers: dict[str, FetchResult]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        self.calls.append((url, user_agent))
        return self.answers.get(url, FetchResult(status=404, error="Not Found"))


def _source(source_id: int = 1, **kw: object) -> Source:
    defaults: dict[str, object] = {
        "source_id": source_id,
        "name": "P",
        "home_url": "https://p.example",
        "rights_level": RightsLevel.BODY_TEXT,
        "jurisdiction": "GB",
        "rate_limit_per_min": 600,
    }
    return Source(**{**defaults, **kw})


def _cache(fetcher: Fetcher, clock: Clock | None = None, **kw: float) -> RobotsCache:
    clock = clock or Clock()
    return RobotsCache(fetcher, Pacer(sleep=lambda _: None, clock=clock), clock=clock, **kw)


ROBOTS = "https://www.aljazeera.com/robots.txt"
OK = FetchResult(status=200, body=AL_JAZEERA)


def test_a_wildcard_rule_matches_the_query_string() -> None:
    """AC2. The rule ScrapeFlow reads as matching nothing."""
    cache = _cache(Fetcher({ROBOTS: OK}))
    source = _source()
    decorated = "https://www.aljazeera.com/news/2026/9/18/x?traffic_source=rss"
    canonical = "https://www.aljazeera.com/news/2026/9/18/x"
    assert cache.allowed(decorated, source) is False
    assert cache.allowed(canonical, source) is True


def test_the_file_is_fetched_once_per_host_not_per_article() -> None:
    """AC2. Re-fetching per article is ~1000 extra requests a day against five files."""
    fetcher = Fetcher({ROBOTS: OK})
    cache = _cache(fetcher)
    source = _source()
    for n in range(50):
        cache.allowed(f"https://www.aljazeera.com/news/{n}", source)
        cache.crawl_delay(f"https://www.aljazeera.com/news/{n}", source)
    assert [url for url, _ in fetcher.calls] == [ROBOTS]


def test_a_missing_file_opens_the_site() -> None:
    fetcher = Fetcher({})  # everything 404s
    cache = _cache(fetcher)
    assert cache.allowed("https://open.example/anything", _source()) is True
    assert cache.crawl_delay("https://open.example/anything", _source()) is None


def test_an_unreachable_file_closes_the_site_when_nothing_is_cached() -> None:
    """AC2. Fail closed: a 5xx or no answer is not permission."""
    for failure in (
        FetchResult(status=503, error="Service Unavailable"),
        FetchResult(status=None, error="ConnectTimeout: timed out"),
    ):
        cache = _cache(Fetcher({ROBOTS: failure}))
        assert cache.allowed("https://www.aljazeera.com/news/x", _source()) is False


def test_an_unreachable_file_falls_back_to_the_cached_copy() -> None:
    """AC2. The copy we hold is what RFC 9309 says to use while the host is down — and it is
    the copy's *rules* that apply, not a blanket allow."""
    fetcher = Fetcher({ROBOTS: OK})
    clock = Clock()
    cache = _cache(fetcher, clock, ttl=100.0, failure_ttl=10.0)
    source = _source()
    assert cache.allowed("https://www.aljazeera.com/news/x", source) is True

    fetcher.answers[ROBOTS] = FetchResult(status=503, error="Service Unavailable")
    clock.now = 101.0  # the good copy has expired; the refetch fails
    assert cache.allowed("https://www.aljazeera.com/news/x", source) is True
    assert cache.allowed("https://www.aljazeera.com/news/x?traffic_source=rss", source) is False
    assert len(fetcher.calls) == 2


def test_a_failure_is_retried_after_its_own_shorter_ttl() -> None:
    fetcher = Fetcher({ROBOTS: FetchResult(status=503, error="Service Unavailable")})
    clock = Clock()
    cache = _cache(fetcher, clock, ttl=100.0, failure_ttl=10.0)
    source = _source()
    url = "https://www.aljazeera.com/news/x"

    assert cache.allowed(url, source) is False
    clock.now = 5.0
    assert cache.allowed(url, source) is False
    assert len(fetcher.calls) == 1, "closed, and not hammering the host to ask again"

    clock.now = 11.0
    fetcher.answers[ROBOTS] = OK
    assert cache.allowed(url, source) is True
    assert len(fetcher.calls) == 2


def test_a_good_copy_is_refetched_after_the_ttl() -> None:
    fetcher = Fetcher({ROBOTS: OK})
    clock = Clock()
    cache = _cache(fetcher, clock, ttl=100.0)
    source = _source()
    cache.allowed("https://www.aljazeera.com/news/x", source)
    clock.now = 99.0
    cache.allowed("https://www.aljazeera.com/news/x", source)
    assert len(fetcher.calls) == 1
    clock.now = 100.0
    cache.allowed("https://www.aljazeera.com/news/x", source)
    assert len(fetcher.calls) == 2


def test_the_robots_fetch_takes_a_slot_from_the_publishers_budget() -> None:
    """AC3. It is a request to the host like any other."""
    slept: list[float] = []
    clock = Clock()
    pacer = Pacer(sleep=slept.append, clock=clock)
    cache = RobotsCache(Fetcher({ROBOTS: OK}), pacer, clock=clock)
    source = _source(rate_limit_per_min=5)

    pacer.acquire(source)  # some earlier request
    cache.allowed("https://www.aljazeera.com/news/x", source)
    assert slept == [12.0]


def test_crawl_delay_is_read_for_the_publishers_agent() -> None:
    cache = _cache(Fetcher({ROBOTS: OK}))
    assert cache.crawl_delay("https://www.aljazeera.com/news/x", _source()) == 7.0


def test_the_publishers_own_user_agent_is_sent_and_matched() -> None:
    rules = b"User-agent: *\nDisallow: /\nUser-agent: Custom\nAllow: /\n"
    fetcher = Fetcher({"https://p.example/robots.txt": FetchResult(status=200, body=rules)})
    cache = _cache(fetcher)
    assert cache.allowed("https://p.example/a", _source(user_agent="Custom/9")) is True
    assert fetcher.calls == [("https://p.example/robots.txt", "Custom/9")]
    assert cache.allowed("https://p.example/a", _source(source_id=2)) is False


def test_two_threads_asking_about_one_host_share_one_fetch() -> None:
    gate = threading.Event()

    class SlowFetcher(Fetcher):
        def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
            gate.wait(timeout=5)
            return super().__call__(url, user_agent=user_agent, headers=headers)

    fetcher = SlowFetcher({ROBOTS: OK})
    cache = RobotsCache(fetcher, Pacer(sleep=lambda _: None))
    source = _source()
    results: list[bool] = []

    def ask() -> None:
        results.append(cache.allowed("https://www.aljazeera.com/news/x", source))

    threads = [threading.Thread(target=ask) for _ in range(2)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(timeout=10)
    assert results == [True, True]
    assert len(fetcher.calls) == 1
