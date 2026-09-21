"""Obtaining the article body inside the acquire stage (MER-23's acceptance criteria).

Against a real PostgreSQL, like ``test_acquire.py``: the claim, the heartbeat and the release
are all row-level behaviour of ``pipeline_work``. The network is a routing fake that records
every request, so "no request was made" is a statement about a list.
"""

import datetime as dt
import json
import logging
from collections.abc import Mapping
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import (
    AcquisitionTier,
    BodyProvenance,
    PipelineState,
    RightsLevel,
    Stage,
    TerminalReason,
)
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import CanonicalRecord, Feed, FeedPollState, PipelineWork, Source
from meridian.ingest.acquire import AcquireReport, handle, run_batch
from meridian.ingest.discovery import run_cycle
from meridian.ingest.extract import extract
from meridian.ingest.fetch import FetchResult
from meridian.ingest.network import Network
from meridian.ingest.normalize import content_hash
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache
from tests.factories import make_article, make_feed, make_source, make_work

pytestmark = pytest.mark.postgres

LEASE = dt.timedelta(minutes=5)
HOST = "https://news.example"
ROBOTS = f"{HOST}/robots.txt"

_PARAS = [
    "The council approved the plan on Tuesday after a debate that ran for three hours.",
    "Opponents said the cost had been understated and asked for an independent review.",
    "The first phase is expected to begin next spring, subject to funding.",
]


def _page(*paragraphs: str, marker: str = "") -> bytes:
    """A page with furniture around the article, and optionally a marker that only the raw
    HTML carries — a comment and a script — so 'the raw page was written nowhere' is testable."""
    body = "".join(f"<p>{p}</p>" for p in (paragraphs or _PARAS))
    return (
        "<!doctype html><html><head><title>Council plan</title>"
        f"<script>window.__m = '{marker}';</script></head>"
        f"<body><!-- {marker} --><nav><a href='/'>Home</a> <a href='/news'>News</a></nav>"
        f"<main><article><h1>Council approves plan</h1>{body}</article></main>"
        "<footer>Copyright 2026 Example Media. All rights reserved.</footer></body></html>"
    ).encode()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Fetcher:
    """Answers per URL, records every call, and can run a hook when a URL is asked for.

    Unknown URLs answer 404 — which for ``robots.txt`` means *open* (RFC 9309), so a test that
    says nothing about robots gets an unrestricted host.
    """

    def __init__(self, answers: dict[str, FetchResult] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[str] = []
        self.on_call: dict[str, Any] = {}

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        self.calls.append(url)
        if url in self.on_call:
            self.on_call[url]()
        return self.answers.get(url, FetchResult(status=404, error="Not Found"))

    @property
    def articles(self) -> list[str]:
        return [url for url in self.calls if not url.endswith("/robots.txt")]


def _network(fetcher: Fetcher, clock: Clock | None = None) -> tuple[Network, Clock]:
    clock = clock or Clock()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    return Network(
        fetcher=fetcher, robots=RobotsCache(fetcher, pacer, clock=clock), pacer=pacer
    ), clock


def _article(
    session: Session,
    *,
    rights: RightsLevel = RightsLevel.BODY_TEXT,
    tier: AcquisitionTier = AcquisitionTier.EXTRACTION,
    url: str = f"{HOST}/news/one",
    guid: str = "one",
    source: Source | None = None,
    **source_kw: Any,
) -> tuple[Source, Feed, CanonicalRecord, PipelineWork]:
    source = source or make_source(
        session, rights_level=rights, rate_limit_per_min=600, **source_kw
    )
    feed = make_feed(session, source, acquisition_tier=tier)
    article = make_article(
        session,
        source,
        guid=guid,
        url=url,
        title="Council approves the plan after a three-hour debate",
        lede="<p>Opponents asked for an <b>independent</b> review.</p>",
        feed_id=feed.feed_id,
    )
    work = make_work(session, stage=Stage.ACQUIRE, article=article)
    session.commit()
    return source, feed, article, work


def _work_rows(session: Session) -> list[PipelineWork]:
    session.expire_all()
    return list(session.scalars(sa.select(PipelineWork)).all())


# ---------------------------------------------------------------- AC1: rights before network


def test_a_headline_only_publisher_causes_zero_requests(app_session: Session) -> None:
    """AC1. The host is open and the page is there; the rights rung is first and says no."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, rights=RightsLevel.HEADLINE_ONLY)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert (report.acquired, report.fetched) == (1, 0)
    assert article.body_text is None and article.content_hash is None
    assert article.pipeline_state is PipelineState.ACQUIRED


@pytest.mark.parametrize(
    "stop",
    [{"enabled": False}, {"permitted_to_ingest": False}],
    ids=["disabled", "not-permitted"],
)
def test_a_stopped_publisher_causes_zero_requests(
    app_session: Session, stop: dict[str, Any]
) -> None:
    """A publisher stopped in the registry gets no request of any kind — the article it already
    gave us continues, headline-only, at the level it was taken under (A-L5)."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, **stop)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert report.acquired == 1
    assert article.body_text is None
    assert article.pipeline_state is PipelineState.ACQUIRED


def test_an_fr_i7_drop_costs_the_publisher_nothing(app_session: Session) -> None:
    """Language is decided before the ladder: a dropped article is never fetched."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    source, feed, _, _ = _article(app_session)
    article = make_article(
        app_session,
        source,
        guid="es",
        url=f"{HOST}/news/es",
        title="Por que Cuba no produce suficiente comida para alimentar a su poblacion",
        feed_id=feed.feed_id,
    )
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    report = handle(app_session, work, network=network, lease=LEASE)

    assert report == AcquireReport(dropped=1)
    assert fetcher.calls == []
    assert article.terminal_reason is TerminalReason.DROPPED_LANGUAGE


# ------------------------------------------------------------------- AC6: the body is stored


def test_a_fetched_body_is_stored_with_provenance_and_hash(app_session: Session) -> None:
    """AC6, first half."""
    page = _page()
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=page)})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.acquired, report.fetched) == (1, 1)
    assert fetcher.articles == [f"{HOST}/news/one"]
    assert article.body_text == extract(page, f"{HOST}/news/one")
    assert article.body_text is not None and _PARAS[0] in article.body_text
    assert article.body_provenance is BodyProvenance.TIER3_EXTRACTED
    assert article.content_hash == content_hash(article.body_text)
    assert article.lede == "Opponents asked for an independent review."
    assert article.language == "en"
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]


def test_a_tier1_body_leaves_the_stage_with_its_hash(app_session: Session) -> None:
    """AC6, second half — the assertion MER-82 says the tier-1 test stopped short of. The body
    arrived with discovery; the stage fetches nothing and computes the hash."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    source = make_source(app_session, rate_limit_per_min=600)
    feed = make_feed(app_session, source, acquisition_tier=AcquisitionTier.FULL_FEED)
    article = make_article(
        app_session,
        source,
        title="Council approves the plan after a three-hour debate",
        body_text=" ".join(_PARAS),
        body_provenance=BodyProvenance.TIER1_FEED,
        feed_id=feed.feed_id,
    )
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert (report.acquired, report.fetched) == (1, 0)
    assert article.body_provenance is BodyProvenance.TIER1_FEED
    assert article.content_hash == content_hash(" ".join(_PARAS))
    # RFC §5.1's invariant, both directions, on the record as it leaves the stage.
    assert (article.content_hash is not None) == (article.body_text is not None)


def test_a_tier1_item_without_a_body_continues_without_one(app_session: Session) -> None:
    """The feed is tier 1 but this item shipped no content. Nothing to fetch, nothing to hash."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.FULL_FEED)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert report.acquired == 1
    assert article.body_text is None and article.content_hash is None


# ----------------------------------------------------------- AC10: tier 0 and the tier-2 route

EC_ARTICLE = "https://ec.europa.eu/commission/presscorner/detail/en/speech_26_1908"
EC_API = (
    "https://ec.europa.eu/commission/presscorner/api/documents?reference=SPEECH/26/1908&language=en"
)
EC_ROBOTS = "https://ec.europa.eu/robots.txt"


def _ec_response(html: str) -> FetchResult:
    document = {"refCd": "SPEECH/26/1908", "docuLanguageResource": {"htmlContent": html}}
    return FetchResult(status=200, body=json.dumps(document).encode())


def test_a_tier0_feed_causes_no_request_whatever_the_rights(app_session: Session) -> None:
    """AC10, second half. The publisher's rights are ``body_text``; the feed says no body is
    obtainable. The record continues with headline and lede only."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.UNAVAILABLE)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert report == AcquireReport(claimed=1, acquired=1)
    assert article.body_text is None and article.body_provenance is None
    assert article.pipeline_state is PipelineState.ACQUIRED


def test_a_tier2_body_comes_through_the_adapter(app_session: Session) -> None:
    """AC10, first half: the API is requested, never the article page, and the stored body is
    the extraction of the API's HTML."""
    html = "".join(f"<p>{p}</p>" for p in _PARAS)
    fetcher = Fetcher({EC_API: _ec_response(html)})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.PUBLISHER_API, url=EC_ARTICLE)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.articles == [EC_API]
    assert (report.acquired, report.fetched, report.adapter_missing) == (1, 1, 0)
    assert article.body_text == extract(html.encode(), EC_ARTICLE)
    assert article.body_text is not None and _PARAS[1] in article.body_text
    assert article.body_provenance is BodyProvenance.TIER2_API
    assert article.content_hash == content_hash(article.body_text)


def test_robots_is_read_for_the_url_actually_requested(app_session: Session) -> None:
    """The article page is allowed and the API path is not; the API is what we would request,
    so the article is robots-blocked. Checked against the page instead, the fetch would go ahead."""
    fetcher = Fetcher(
        {
            EC_ROBOTS: FetchResult(
                status=200, body=b"User-agent: *\nDisallow: /commission/presscorner/api/\n"
            ),
            EC_API: _ec_response("<p>never reached</p>"),
        }
    )
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.PUBLISHER_API, url=EC_ARTICLE)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == [EC_ROBOTS]
    assert (report.acquired, report.robots_blocked, report.fetched) == (1, 1, 0)
    assert article.body_text is None


def test_a_tier2_host_with_no_adapter_continues_without_a_body(app_session: Session) -> None:
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.PUBLISHER_API)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == []
    assert (report.acquired, report.adapter_missing) == (1, 1)
    assert article.body_text is None
    assert article.pipeline_state is PipelineState.ACQUIRED


def test_a_tier2_response_in_the_wrong_shape_is_an_adapter_miss(app_session: Session) -> None:
    """The API answered 200 with something that is not what it promised. Nothing is written
    from a guess; the miss is counted so a changed API shows up every cycle."""
    fetcher = Fetcher({EC_API: FetchResult(status=200, body=b'{"error": "moved"}')})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session, tier=AcquisitionTier.PUBLISHER_API, url=EC_ARTICLE)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.articles == [EC_API]
    assert (report.acquired, report.adapter_missing, report.fetched) == (1, 1, 0)
    assert article.body_text is None and article.body_provenance is None


# ------------------------------------------------------ robots, pacing, and the fetch outcomes


def test_a_robots_disallowed_page_is_not_fetched_and_is_counted(app_session: Session) -> None:
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=200, body=b"User-agent: *\nDisallow: /news/\n"),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
        }
    )
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls == [ROBOTS]
    assert (report.acquired, report.robots_blocked, report.fetched) == (1, 1, 0)
    assert article.body_text is None
    assert article.pipeline_state is PipelineState.ACQUIRED


def test_crawl_delay_spaces_the_article_fetches(app_session: Session) -> None:
    """AC3 on the acquire side: the site's ``Crawl-delay`` is the floor under the publisher's
    own rate. Two articles from one host at 600/min wait 7 s each, not 0.1 s."""
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=200, body=b"User-agent: *\nCrawl-delay: 7\n"),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
            f"{HOST}/news/two": FetchResult(status=200, body=_page()),
        }
    )
    network, clock = _network(fetcher)
    source, feed, _, _ = _article(app_session)
    second = make_article(
        app_session,
        source,
        guid="two",
        url=f"{HOST}/news/two",
        title="Mayor defends the spending in a second statement",
        feed_id=feed.feed_id,
    )
    make_work(app_session, stage=Stage.ACQUIRE, article=second)
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report.fetched == 2
    assert clock.slept == [7.0, 7.0]


def test_a_wait_past_the_lease_releases_the_row_until_the_slot(app_session: Session) -> None:
    """The publisher's next slot is further off than the lease. Sleeping would overstay the
    claim, so the row goes back with ``next_attempt_at`` at the slot — and however often this
    recurs it is never a strike: a pacing wait is not a failure."""
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=200, body=b"User-agent: *\nCrawl-delay: 60\n"),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
        }
    )
    network, clock = _network(fetcher)
    _, _, article, work = _article(app_session)
    work.attempts = work_queue.MAX_ATTEMPTS + 5
    app_session.commit()

    before = dt.datetime.now(dt.UTC)
    report = run_batch(app_session, network=network, lease=dt.timedelta(seconds=30))

    assert fetcher.articles == []
    assert clock.slept == []
    assert report == AcquireReport(claimed=1, fetch_deferred=1)
    (row,) = _work_rows(app_session)
    assert row.claimed_at is None and row.claimed_by is None
    assert row.dead_lettered_at is None
    assert row.next_attempt_at >= before + dt.timedelta(seconds=59)
    assert row.last_error is not None and "pacing" in row.last_error
    assert article.pipeline_state is PipelineState.DISCOVERED


@pytest.mark.parametrize(
    "answer",
    [
        FetchResult(status=503, error="Service Unavailable"),
        FetchResult(status=None, error="ReadTimeout"),
    ],
    ids=["503", "no-answer"],
)
def test_a_transient_failure_releases_the_row_with_backoff(
    app_session: Session, answer: FetchResult
) -> None:
    """AC4, first half. And the release leaves the record exactly as it found it — the lede is
    still the publisher's markup, not stripped, so the retry strips it once."""
    fetcher = Fetcher({f"{HOST}/news/one": answer})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    before = dt.datetime.now(dt.UTC)
    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, fetch_deferred=1)
    (row,) = _work_rows(app_session)
    assert row.claimed_at is None and row.claimed_by is None
    assert row.attempts == 1
    assert row.next_attempt_at >= before + work_queue.backoff_after(1)
    assert row.last_error is not None and (answer.error or "") in row.last_error
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.DISCOVERED
    assert article.lede == "<p>Opponents asked for an <b>independent</b> review.</p>"
    assert article.language is None and article.body_text is None
    # Not due yet: the next batch does not see it.
    assert run_batch(app_session, network=network, lease=LEASE).claimed == 0


def test_a_4xx_continues_without_a_body_and_is_counted_as_refused(app_session: Session) -> None:
    """AC4, second half. A 403 at the edge is the publisher's decision about the request —
    a different thing from a robots block and from a failure, and recorded as such."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=403, error="Forbidden")})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.acquired, report.fetch_refused, report.failed, report.robots_blocked) == (
        1,
        1,
        0,
        0,
    )
    assert article.body_text is None
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]


def test_an_error_page_with_a_body_is_not_extracted(app_session: Session) -> None:
    """Only a 200 body reaches extraction. A 404 page extracts to *something* — measured, 5,300
    characters of cookie policy on one roster host — and no length rule can tell it from an
    article. The status code is the filter."""
    fetcher = Fetcher(
        {f"{HOST}/news/one": FetchResult(status=404, body=_page(), error="Not Found")}
    )
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.fetch_refused, report.fetched) == (1, 0)
    assert article.body_text is None and article.content_hash is None


def test_a_page_with_nothing_extractable_is_counted_empty(app_session: Session) -> None:
    shell = b"<html><body><div id='app'></div><script>boot()</script></body></html>"
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=shell)})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.acquired, report.extract_empty, report.fetched) == (1, 1, 0)
    assert article.body_text is None
    assert article.pipeline_state is PipelineState.ACQUIRED


def test_the_penultimate_transient_failure_is_released_again(app_session: Session) -> None:
    """AC4, third half, the attempt before last: still a release, still the same row."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=503, error="Service Unavailable")})
    network, _ = _network(fetcher)
    _, _, article, work = _article(app_session)
    work.attempts = work_queue.MAX_ATTEMPTS - 2
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, fetch_deferred=1)
    (row,) = _work_rows(app_session)
    assert row.stage is Stage.ACQUIRE and row.attempts == work_queue.MAX_ATTEMPTS - 1
    assert row.claimed_at is None and row.next_attempt_at > dt.datetime.now(dt.UTC)
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.DISCOVERED
    assert article.terminal_reason is None


def test_the_final_transient_failure_continues_the_article_without_a_body(
    app_session: Session,
) -> None:
    """AC4, third half: on the last attempt the fetch is given up, not the article. It moves on
    headline-only — the same outcome as every other rung that yields no body — rather than
    dying: its headline is real, and a cluster should count it whether or not its page is up.
    Falsified by the neighbouring test: one attempt earlier, the same 503 is a release."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=503, error="Service Unavailable")})
    network, _ = _network(fetcher)
    _, _, article, work = _article(app_session)
    work.attempts = work_queue.MAX_ATTEMPTS - 1
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, acquired=1, fetch_abandoned=1)
    # Advanced like any acquired article: the acquire row is gone, the successor is queued.
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert article.terminal_reason is None
    assert article.body_text is None and article.body_provenance is None
    assert article.content_hash is None
    # The final write is a real one: the lede is stripped and the language recorded.
    assert article.lede == "Opponents asked for an independent review."
    assert article.language == "en"
    # Not retried: the next batch has nothing at this stage to claim.
    assert run_batch(app_session, network=network, lease=LEASE).claimed == 0


# ------------------------------------------------------------- AC3: one budget across both jobs

FEED_URL = f"{HOST}/feed.xml"
FEED_XML = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Ex</title>'
    "<item><title>Council approves the plan after a three-hour debate</title>"
    f"<link>{HOST}/news/one</link><guid>one</guid>"
    "<description>Opponents asked for a review.</description></item></channel></rss>"
).encode()


def test_discovery_and_acquire_draw_on_one_budget_per_publisher(app_session: Session) -> None:
    """AC3's "whichever job issues them". The feed poll and the article fetch that follows it
    go to one host; through one ``Network`` the second waits out the gap the first started.
    With a pacer per job each would see a first request and wait nothing — two requests to
    the publisher inside one second, each job honest on its own."""
    fetcher = Fetcher(
        {
            FEED_URL: FetchResult(status=200, body=FEED_XML),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
        }
    )
    network, clock = _network(fetcher)
    source = make_source(app_session, rate_limit_per_min=5)
    make_feed(app_session, source, url=FEED_URL, acquisition_tier=AcquisitionTier.EXTRACTION)
    app_session.commit()

    cycle = run_cycle(app_session, network, clock=clock)
    batch = run_batch(app_session, network=network, lease=LEASE)

    assert (cycle.discovered, batch.fetched) == (1, 1)
    assert fetcher.calls == [ROBOTS, FEED_URL, f"{HOST}/news/one"]
    # robots.txt at t=0, the feed 12 s later, the article 12 s after that.
    assert clock.slept == [12.0, 12.0]


# ----------------------------------------------------------- AC5: the heartbeat, and AC7, AC9


def _watcher(app_session: Session) -> Session:
    """Another connection — what a second worker, or a DBA, sees between our commits."""
    return Session(app_session.get_bind(), expire_on_commit=False)


def _three_articles(app_session: Session, fetcher: Fetcher) -> list[CanonicalRecord]:
    source, feed, first, _ = _article(app_session)
    articles = [first]
    for guid in ("two", "three"):
        article = make_article(
            app_session,
            source,
            guid=guid,
            url=f"{HOST}/news/{guid}",
            title=f"Council approves the plan after a debate, part {guid}",
            feed_id=feed.feed_id,
        )
        make_work(app_session, stage=Stage.ACQUIRE, article=article)
        articles.append(article)
    app_session.commit()
    for article in articles:
        fetcher.answers[article.url_canonical] = FetchResult(status=200, body=_page())
    return articles


def test_rows_still_held_are_restamped_after_every_article(app_session: Session) -> None:
    """AC5. The claim stamps every row once; without the heartbeat the third article's row
    still carries that stamp when its turn comes, and a 50-row batch paced at 12 s has rows
    past a 300 s lease before the batch is half done. With it, each row's stamp when fetched is
    later than the previous article's — it was renewed after that article finished."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    articles = _three_articles(app_session, fetcher)
    stamps: list[dt.datetime] = []

    def record(article_id: int) -> None:
        with _watcher(app_session) as db:
            stamp = db.scalar(
                sa.select(PipelineWork.claimed_at).where(PipelineWork.article_id == article_id)
            )
        assert stamp is not None
        stamps.append(stamp)

    for article in articles:
        fetcher.on_call[article.url_canonical] = lambda a=article.article_id: record(a)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report.fetched == 3
    assert len(stamps) == 3
    assert stamps[0] < stamps[1] < stamps[2], stamps


def test_a_row_another_worker_reclaimed_is_dropped_before_it_is_fetched(
    app_session: Session,
) -> None:
    """AC5, second half. While we fetch the first article, another worker takes the third row.
    The heartbeat after the first article returns two ids, not three, and the third URL is
    never requested — nothing is done that ``advance()`` would then refuse."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    first, second, third = _three_articles(app_session, fetcher)

    def steal() -> None:
        with _watcher(app_session) as db:
            db.execute(
                sa.update(PipelineWork)
                .where(PipelineWork.article_id == third.article_id)
                .values(claimed_by="acquire@elsewhere:1", claimed_at=dt.datetime.now(dt.UTC))
            )
            db.commit()

    fetcher.on_call[first.url_canonical] = steal

    report = run_batch(app_session, network=network, lease=LEASE)

    assert third.url_canonical not in fetcher.calls
    assert (report.claimed, report.acquired, report.stale, report.failed) == (3, 2, 1, 0)
    app_session.expire_all()
    states = {
        article.article_id: app_session.get(CanonicalRecord, article.article_id)
        for article in (first, second, third)
    }
    assert {a: r.pipeline_state for a, r in states.items() if r is not None} == {
        first.article_id: PipelineState.ACQUIRED,
        second.article_id: PipelineState.ACQUIRED,
        third.article_id: PipelineState.DISCOVERED,
    }


def test_the_batch_alternates_publishers(app_session: Session) -> None:
    """Round-robin over publishers, so one publisher's gap is spent on the other's requests.
    The rows are created a1, a2, b1, b2 — the order the claim returns them in."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    urls: list[str] = []
    for name, host in (("A", HOST), ("B", "https://other.example")):
        source, feed, first, _ = _article(
            app_session, url=f"{host}/news/{name}1", guid=f"{name}1", name=name
        )
        second = make_article(
            app_session,
            source,
            guid=f"{name}2",
            url=f"{host}/news/{name}2",
            title=f"Council approves the plan after a debate, {name}2",
            feed_id=feed.feed_id,
        )
        make_work(app_session, stage=Stage.ACQUIRE, article=second)
        app_session.commit()
        urls += [first.url_canonical, second.url_canonical]
    for url in urls:
        fetcher.answers[url] = FetchResult(status=200, body=_page())

    run_batch(app_session, network=network, lease=LEASE)

    hosts = [url.split("/")[2] for url in fetcher.articles]
    assert hosts == ["news.example", "other.example", "news.example", "other.example"]


def test_no_transaction_is_held_open_across_the_fetch(app_session: Session) -> None:
    """A transaction open across a network call pins the database's oldest xmin for as long as
    the publisher takes to answer. The stage closes its read transaction before the first
    request and opens a new one for the write."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    _article(app_session)
    open_during_fetch: list[bool] = []
    fetcher.on_call[f"{HOST}/news/one"] = lambda: open_during_fetch.append(
        app_session.in_transaction()
    )

    run_batch(app_session, network=network, lease=LEASE)

    assert open_during_fetch == [False]


def test_the_raw_page_is_written_nowhere(
    app_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """AC7. A marker that only the raw HTML carries — inside a comment and a script — is in no
    column of any table the stage touches, and in no log line. The body was stored, so the page
    did pass through."""
    marker = "RAW-HTML-MARKER-7f3a9c"
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page(marker=marker))})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    with caplog.at_level(logging.DEBUG):
        report = run_batch(app_session, network=network, lease=LEASE)

    assert report.fetched == 1
    assert article.body_text is not None and _PARAS[0] in article.body_text
    app_session.expire_all()
    for model in (CanonicalRecord, PipelineWork, FeedPollState, Feed, Source):
        for row in app_session.scalars(sa.select(model)).all():
            for column in model.__mapper__.columns:
                value = getattr(row, column.key)
                assert not (isinstance(value, str) and marker in value), (model, column.key)
    assert marker not in caplog.text


def test_one_raising_article_does_not_stop_the_batch(app_session: Session) -> None:
    """AC9, with a network call inside the handler: the fetcher raising for one article is
    contained like any other failure, and the other article is fetched and advanced."""
    fetcher = Fetcher()
    network, _ = _network(fetcher)
    first, second, third = _three_articles(app_session, fetcher)

    def explode() -> None:
        raise RuntimeError("connection pool exhausted")

    fetcher.on_call[second.url_canonical] = explode

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.claimed, report.acquired, report.failed) == (3, 2, 1)
    [row] = [r for r in _work_rows(app_session) if r.stage is Stage.ACQUIRE]
    assert row.article_id == second.article_id
    assert row.last_error is not None and "connection pool exhausted" in row.last_error
    for article in (first, third):
        refreshed = app_session.get(CanonicalRecord, article.article_id)
        assert refreshed is not None and refreshed.body_text is not None


# ------------------------------------------------- the registry can move while a fetch is in flight


def _operator_writes(session: Session, source_id: int, field: str) -> None:
    """The operator's write, through the compare-and-set setters the admin surface uses."""
    from meridian.db import sources as sources_repo

    with _watcher(session) as db:
        src = db.get(Source, source_id)
        assert src is not None
        if field == "rights_level":
            sources_repo.set_rights_level(
                db, source_id, level=RightsLevel.HEADLINE_ONLY, expected_updated_at=src.updated_at
            )
        elif field == "permitted_to_ingest":
            sources_repo.set_permitted_to_ingest(
                db, source_id, value=False, expected_updated_at=src.updated_at
            )
        else:
            sources_repo.set_enabled(db, source_id, value=False, expected_updated_at=src.updated_at)
        db.commit()


@pytest.mark.parametrize("field", ["rights_level", "permitted_to_ingest", "enabled"])
def test_a_publisher_stopped_or_downgraded_during_the_fetch_has_its_body_withheld(
    app_session: Session, field: str
) -> None:
    """The gates are read before the network and the registry can move while the page is being
    fetched — a pacing wait alone can be a whole lease. The body is written under the answer in
    force at the write (RFC §5.2: the rights predicate is read wherever a body is written), so
    an operator's stop or downgrade mid-fetch means the fetched text is not held. The article
    continues, headline-only, and the report says a body was obtained and withheld."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    source, _, article, _ = _article(app_session)
    fetcher.on_call[f"{HOST}/news/one"] = lambda: _operator_writes(
        app_session, source.source_id, field
    )

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.articles == [f"{HOST}/news/one"], "the request itself was legitimate"
    assert report == AcquireReport(claimed=1, acquired=1, withheld=1)
    with _watcher(app_session) as db:
        body = db.scalar(
            sa.select(CanonicalRecord.body_text).where(
                CanonicalRecord.article_id == article.article_id
            )
        )
        state = db.scalar(
            sa.select(CanonicalRecord.pipeline_state).where(
                CanonicalRecord.article_id == article.article_id
            )
        )
    assert body is None
    assert state is PipelineState.ACQUIRED
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]


# ----------------------------------------------------- handing a row back that is no longer ours


def test_release_of_a_row_another_worker_completed_is_counted_stale(
    app_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Our lease expires mid-fetch; another worker reclaims, fetches and advances the row; then
    our page answers 503 and we go to release it. The row is not ours to give back — and it is
    gone. That is ``stale``, the same outcome ``advance()`` reports on the 200 branch, not a
    failure with a traceback for someone to chase."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=503, error="Service Unavailable")})
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    def other_worker_completes() -> None:
        with _watcher(app_session) as db:
            db.execute(
                sa.update(PipelineWork).values(claimed_at=dt.datetime.now(dt.UTC) - LEASE * 2)
            )
            db.commit()
            (theirs,) = work_queue.claim(db, stage=Stage.ACQUIRE, worker="acquire@b:2", lease=LEASE)
            record = db.get(CanonicalRecord, theirs.article_id)
            assert record is not None
            record.body_text = " ".join(_PARAS)
            record.body_provenance = BodyProvenance.TIER3_EXTRACTED
            work_queue.advance(db, theirs)
            db.commit()

    fetcher.on_call[f"{HOST}/news/one"] = other_worker_completes

    with caplog.at_level(logging.INFO):
        report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, stale=1), f"{report}\n{caplog.text}"
    assert "Traceback" not in caplog.text
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert article.body_text == " ".join(_PARAS), "their body stands"


# ------------------------------------------------ a wait that is not the row's fault is free


def test_pacing_releases_leave_the_schedule_intact_for_the_first_real_failure(
    app_session: Session,
) -> None:
    """``claim()`` counts every claim; a release for a pacing wait gives the count back. Four
    pacing releases, then the slot opens and the page's first 503 is met with a release and
    the first backoff — not the give-up. Deleting the give-back makes the fourth release leave
    ``attempts`` at 4 and the 503 is given up on."""
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=200, body=b"User-agent: *\nCrawl-delay: 60\n"),
            f"{HOST}/news/one": FetchResult(status=503, error="Service Unavailable"),
        }
    )
    network, clock = _network(fetcher)
    _article(app_session)
    short_lease = dt.timedelta(seconds=30)

    for _ in range(work_queue.MAX_ATTEMPTS - 1):
        app_session.execute(sa.update(PipelineWork).values(next_attempt_at=dt.datetime.now(dt.UTC)))
        app_session.commit()
        assert run_batch(app_session, network=network, lease=short_lease) == AcquireReport(
            claimed=1, fetch_deferred=1
        )
        (row,) = _work_rows(app_session)
        assert row.attempts == 0, "a pacing release is not a strike"
        assert row.last_error is not None and "pacing" in row.last_error
    assert fetcher.articles == []

    clock.now += 120.0
    app_session.execute(sa.update(PipelineWork).values(next_attempt_at=dt.datetime.now(dt.UTC)))
    app_session.commit()
    before = dt.datetime.now(dt.UTC)

    report = run_batch(app_session, network=network, lease=short_lease)

    assert fetcher.articles == [f"{HOST}/news/one"]
    assert report == AcquireReport(claimed=1, fetch_deferred=1)
    (row,) = _work_rows(app_session)
    assert row.attempts == 1
    assert row.next_attempt_at >= before + work_queue.backoff_after(1)


# ---------------------------------------------------- the hash invariant holds for a dropped record


def test_a_dropped_tier1_article_keeps_the_hash_invariant(app_session: Session) -> None:
    """A feed-shipped body arrives without a hash and this stage adds it. A record FR-I7 then
    drops leaves through ``terminate()``, body and all — and RFC §5.1's ``content_hash``
    present exactly when ``body_text`` is holds at rest, not only for records that continue."""
    network, _ = _network(Fetcher())
    source = make_source(app_session, rate_limit_per_min=600)
    feed = make_feed(app_session, source, acquisition_tier=AcquisitionTier.FULL_FEED)
    body = "El gobierno cubano reconoce que la produccion agricola ha caido."
    article = make_article(
        app_session,
        source,
        guid="es",
        url=f"{HOST}/news/es",
        title="Por que Cuba no produce suficiente comida para alimentar a su poblacion",
        body_text=body,
        body_provenance=BodyProvenance.TIER1_FEED,
        feed_id=feed.feed_id,
    )
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, dropped=1)
    app_session.refresh(article)
    assert article.terminal_reason is TerminalReason.DROPPED_LANGUAGE
    assert article.body_text == body
    assert article.content_hash == content_hash(body)


# --------------------------------------------- a robots.txt outage is an outage, not an answer


def test_an_unreachable_robots_txt_defers_the_fetch_and_the_schedule_ends_it(
    app_session: Session,
) -> None:
    """RFC 9309 closes a host whose robots.txt cannot be read and nothing earlier is held. For
    this stage that must not mean the article continues without a body at once — the host was
    down, not refusing — so the row goes back until the cache will ask again. But the host
    answered 5xx to a request we made, which is what a page 503 is, so it is a strike: an
    outage that outlasts the schedule ends as a fetch given up, and the article continues,
    rather than every article of the publisher being held in this stage for as long as the
    outage lasts, owed and unreadable and invisible to the reconciler."""
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=503, error="Service Unavailable"),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
        }
    )
    network, clock = _network(fetcher)
    _, _, article, _ = _article(app_session)

    for attempt in range(1, work_queue.MAX_ATTEMPTS):
        before = dt.datetime.now(dt.UTC)
        report = run_batch(app_session, network=network, lease=LEASE)
        assert report == AcquireReport(claimed=1, fetch_deferred=1), attempt
        (row,) = _work_rows(app_session)
        assert row.attempts == attempt, "an outage the host answered with is a strike"
        assert row.last_error is not None and "robots.txt" in row.last_error
        # No sooner than the cache's own retry (10 min), which outlasts these backoffs.
        assert before + dt.timedelta(minutes=9) <= row.next_attempt_at
        assert row.next_attempt_at <= before + dt.timedelta(minutes=11)
        clock.now += 601.0
        app_session.execute(sa.update(PipelineWork).values(next_attempt_at=dt.datetime.now(dt.UTC)))
        app_session.commit()
    assert fetcher.articles == [], "closed: the page is never asked for"
    assert fetcher.calls.count(ROBOTS) == work_queue.MAX_ATTEMPTS - 1

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, acquired=1, fetch_abandoned=1)
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.ACQUIRED and article.body_text is None


def test_a_robots_outage_is_read_once_per_rung(app_session: Session) -> None:
    """Whether we may ask, why not, and the site's spacing come from one read of the cache. Two
    reads can straddle the entry's expiry: the second fetches again through the pacer, taking
    a slot, and may answer for a different file than the first — a host back up between the
    two reads would be counted as a rule against us, for good. Under a clock that ticks past
    the outage TTL on every read, a rung still fetches robots.txt exactly once."""

    class Ticking(Clock):
        def __call__(self) -> float:
            self.now += 1.0
            return self.now

    fetcher = Fetcher({ROBOTS: FetchResult(status=503, error="Service Unavailable")})
    clock = Ticking()
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    network = Network(
        fetcher=fetcher,
        robots=RobotsCache(fetcher, pacer, clock=clock, failure_ttl=0.5),
        pacer=pacer,
    )
    _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, fetch_deferred=1)
    assert fetcher.calls.count(ROBOTS) == 1


# ---------------------------------------------------- a body over the cap is refused, not retried


def test_a_body_over_the_cap_is_refused_not_retried(app_session: Session) -> None:
    """The fetcher refuses to read past the cap and reports no status. Unlike a timeout, asking
    again reads the same bytes; the article continues without a body, once, counted as
    refused, and nothing walks the backoff schedule five times over a 5 MB page."""
    fetcher = Fetcher(
        {
            f"{HOST}/news/one": FetchResult(
                status=None, error="body exceeded 5242880 bytes", retryable=False
            )
        }
    )
    network, _ = _network(fetcher)
    _, _, article, _ = _article(app_session)

    report = run_batch(app_session, network=network, lease=LEASE)

    assert report == AcquireReport(claimed=1, acquired=1, fetch_refused=1)
    assert [row.stage for row in _work_rows(app_session)] == [Stage.CLASSIFY]
    app_session.refresh(article)
    assert article.pipeline_state is PipelineState.ACQUIRED and article.body_text is None


def test_a_cached_robots_rule_outlives_an_outage_and_is_still_a_rule(app_session: Session) -> None:
    """The host published ``Disallow: /news/`` and then went down. RFC 9309 says keep using the
    copy we hold — and a disallow from that copy is the publisher's rule, so the article
    continues headline-only and is counted as robots-blocked rather than deferred as an
    outage. Only an outage with nothing held is an outage."""
    fetcher = Fetcher(
        {
            ROBOTS: FetchResult(status=200, body=b"User-agent: *\nDisallow: /news/\n"),
            f"{HOST}/news/one": FetchResult(status=200, body=_page()),
            f"{HOST}/news/two": FetchResult(status=200, body=_page()),
        }
    )
    network, clock = _network(fetcher)
    source, _, _first, _ = _article(app_session)
    assert run_batch(app_session, network=network, lease=LEASE) == AcquireReport(
        claimed=1, acquired=1, robots_blocked=1
    )

    # The copy's TTL expires and the host is now down: the cached rule is what answers.
    clock.now += 25 * 3600
    fetcher.answers[ROBOTS] = FetchResult(status=503, error="Service Unavailable")
    _article(app_session, source=source, url=f"{HOST}/news/two", guid="two")

    report = run_batch(app_session, network=network, lease=LEASE)

    assert fetcher.calls.count(ROBOTS) == 2, "the refresh was attempted"
    assert fetcher.articles == []
    assert report == AcquireReport(claimed=1, acquired=1, robots_blocked=1)


# ------------------------------------ the re-read waits for an operator's uncommitted downgrade


def test_the_re_read_waits_for_an_operators_uncommitted_downgrade_and_sees_it(
    app_session: Session,
) -> None:
    """The operator's transaction holds ``FOR UPDATE`` on the publisher — the downgrade flushed,
    not yet committed — when our write transaction re-reads the gates. A plain refresh would
    not wait: under MVCC it reads the last committed row, still ``body_text``, and the body is
    stored a millisecond before the downgrade lands. ``FOR SHARE`` waits for their commit and
    sees ``headline_only``. ``lock_timeout`` on our side turns a deadlock into an error rather
    than a hang; there is none, because the operator touches only ``source``."""
    import threading
    import time

    from meridian.db import sources as sources_repo

    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=200, body=_page())})
    network, _ = _network(fetcher)
    source, _, article, _ = _article(app_session)
    source_id = source.source_id
    engine = app_session.get_bind()
    assert isinstance(engine, sa.Engine)
    operator = Session(engine, expire_on_commit=False)
    timing: dict[str, float] = {}
    threads: list[threading.Thread] = []

    def take_the_lock_then_commit_later() -> None:
        src = operator.get(Source, source_id)
        assert src is not None
        sources_repo.set_rights_level(
            operator, source_id, level=RightsLevel.HEADLINE_ONLY, expected_updated_at=src.updated_at
        )  # FOR UPDATE held, flushed, not committed

        def commit_later() -> None:
            time.sleep(0.4)
            # Stamped before the call: our FOR SHARE can return the instant the commit lands,
            # which is before this thread gets control back from commit().
            timing["commit_started"] = time.monotonic()
            operator.commit()

        thread = threading.Thread(target=commit_later)
        threads.append(thread)
        thread.start()

    fetcher.on_call[f"{HOST}/news/one"] = take_the_lock_then_commit_later

    def before(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FOR SHARE" in statement:
            timing["share_started"] = time.monotonic()

    def after(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if "FOR SHARE" in statement:
            timing["share_returned"] = time.monotonic()

    sa.event.listen(engine, "before_cursor_execute", before)
    sa.event.listen(engine, "after_cursor_execute", after)
    app_session.execute(sa.text("SET lock_timeout = '5s'"))
    app_session.commit()
    try:
        report = run_batch(app_session, network=network, lease=LEASE)
    finally:
        sa.event.remove(engine, "before_cursor_execute", before)
        sa.event.remove(engine, "after_cursor_execute", after)
        for thread in threads:
            thread.join(timeout=5)
        operator.close()

    assert "share_started" in timing, "the re-read is FOR SHARE"
    assert timing["share_returned"] >= timing["commit_started"], "it waited for their commit"
    assert report == AcquireReport(claimed=1, acquired=1, withheld=1)
    with _watcher(app_session) as db:
        assert (
            db.scalar(
                sa.select(CanonicalRecord.body_text).where(
                    CanonicalRecord.article_id == article.article_id
                )
            )
            is None
        )
