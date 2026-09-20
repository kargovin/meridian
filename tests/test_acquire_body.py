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
from meridian.ingest.acquire import AcquireReport, Network, handle, run_batch
from meridian.ingest.extract import extract
from meridian.ingest.fetch import FetchResult
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


@pytest.mark.parametrize(
    ("attempts_before", "dead_lettered"),
    [(work_queue.MAX_ATTEMPTS - 2, 0), (work_queue.MAX_ATTEMPTS - 1, 1)],
    ids=["released-again", "dead-lettered"],
)
def test_the_final_transient_failure_dead_letters_the_row(
    app_session: Session, attempts_before: int, dead_lettered: int
) -> None:
    """AC4, third half: the row is kept as the trace **and** the article is marked terminal —
    one without the other and the reconciler resurrects it."""
    fetcher = Fetcher({f"{HOST}/news/one": FetchResult(status=503, error="Service Unavailable")})
    network, _ = _network(fetcher)
    _, _, article, work = _article(app_session)
    work.attempts = attempts_before
    app_session.commit()

    report = run_batch(app_session, network=network, lease=LEASE)

    assert (report.fetch_deferred, report.dead_lettered) == (1 - dead_lettered, dead_lettered)
    (row,) = _work_rows(app_session)
    assert row.attempts == attempts_before + 1
    assert (row.dead_lettered_at is not None) is bool(dead_lettered)
    app_session.refresh(article)
    assert (article.terminal_reason is TerminalReason.FAILED) is bool(dead_lettered)
    assert article.pipeline_state is PipelineState.DISCOVERED
    if dead_lettered:
        # Out of the queue for good: a later batch does not claim it.
        row.next_attempt_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        app_session.commit()
        assert run_batch(app_session, network=network, lease=LEASE).claimed == 0


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
