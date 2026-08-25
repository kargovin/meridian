"""One discovery cycle (FR-I1) — MER-16's acceptance criteria.

No network: ``run_cycle`` takes its fetcher as an argument, so these drive the real cycle
against feeds we author here.
"""

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from meridian_contract import AcquisitionTier, DiscoveryMethod, PipelineState, Stage
from sqlalchemy.orm import Session

from meridian.db import poll_state
from meridian.db.models import CanonicalRecord, Feed, PipelineWork
from meridian.ingest.discovery import run_cycle
from meridian.ingest.fetch import DEFAULT_USER_AGENT, FetchResult
from tests.factories import make_feed, make_source

pytestmark = pytest.mark.postgres


def feed_xml(*items: tuple[str, str], description: str = "A teaser.") -> bytes:
    entries = "".join(
        f"<item><title>{title}</title><link>https://x.example/{guid}</link>"
        f"<guid>{guid}</guid><pubDate>Tue, 25 Aug 2026 09:14:02 GMT</pubDate>"
        f"<description>{description}</description></item>"
        for guid, title in items
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Ex</title>'
        f"{entries}</channel></rss>"
    ).encode()


ONE_ITEM = feed_xml(("g1", "Floods displace thousands"))
THREE_ITEMS = feed_xml(("g1", "One"), ("g2", "Two"), ("g3", "Three"))


class FakeFetcher:
    """Answers per URL, and records what it was asked.

    ``etag`` makes it behave like a real publisher: once it has handed one out, a request
    carrying it back gets a 304 with no body.
    """

    def __init__(
        self,
        bodies: Mapping[str, bytes | Exception | FetchResult],
        *,
        etag: str | None = None,
        on_call: Callable[[], None] | None = None,
    ) -> None:
        self._bodies = bodies
        self._etag = etag
        self._on_call = on_call
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        self.calls.append((url, user_agent, dict(headers)))
        if self._on_call is not None:
            self._on_call()
        answer = self._bodies.get(url)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, FetchResult):
            return answer
        if answer is None:
            return FetchResult(status=404, error="Not Found")
        if self._etag and headers.get("If-None-Match") == self._etag:
            return FetchResult(status=304)
        return FetchResult(status=200, body=answer, etag=self._etag)


def _feed_with_source(
    session: Session, *, url: str = "https://feeds.example/a.xml", **kw: Any
) -> Feed:
    source = make_source(session, **kw.pop("source", {}))
    feed = make_feed(session, source, url=url, **kw)
    session.commit()
    return feed


def articles(session: Session) -> list[CanonicalRecord]:
    session.expire_all()
    return list(session.query(CanonicalRecord).order_by(CanonicalRecord.guid).all())


# --------------------------------------------------------------------------- AC1


def test_repolling_the_same_feed_creates_each_item_once(app_session: Session) -> None:
    """AC1. A feed is a standing window, not a mailbox: the same article is served for hours.

    Twenty polls is a bit over an hour and a half at the default cadence.

    ⚠️ The row count alone does NOT test this, and asserting only on it passes while
    idempotency is entirely broken. Without ``ON CONFLICT DO NOTHING`` the insert raises, AC3's
    per-feed except catches it, the feed rolls back, and the table still holds three rows —
    green, for the opposite of the intended reason. What separates the two is that re-polling
    must be a *successful* poll discovering nothing, not a failed one being undone.
    """
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: THREE_ITEMS})

    first = run_cycle(app_session, fetcher, sleep=lambda _: None)
    assert (first.discovered, first.failed) == (3, 0)

    for _ in range(19):
        again = run_cycle(app_session, fetcher, sleep=lambda _: None)
        assert (again.discovered, again.failed) == (0, 0)

    assert len(articles(app_session)) == 3


def test_an_item_is_enqueued_exactly_once_however_often_it_is_seen(
    app_session: Session,
) -> None:
    """The enqueue is conditional on the insert. Were it not, a re-poll would either duplicate
    work rows or collide with the partial unique index and fail the whole feed.
    """
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    for _ in range(5):
        report = run_cycle(app_session, fetcher, sleep=lambda _: None)
        assert report.failed == 0

    work = app_session.query(PipelineWork).all()
    assert len(work) == 1
    assert work[0].stage is Stage.ACQUIRE


def test_a_new_item_appearing_later_is_the_only_one_stored(app_session: Session) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})
    run_cycle(app_session, fetcher, sleep=lambda _: None)

    fetcher = FakeFetcher({feed.url: THREE_ITEMS})
    report = run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert report.discovered == 2
    assert len(articles(app_session)) == 3


def test_a_discovered_record_carries_what_the_feed_said(app_session: Session) -> None:
    feed = _feed_with_source(app_session)
    run_cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}), sleep=lambda _: None)

    article = articles(app_session)[0]
    assert article.guid == "g1"
    assert article.title == "Floods displace thousands"
    assert article.url_canonical == "https://x.example/g1"
    assert article.published_at is not None
    assert article.pipeline_state is PipelineState.DISCOVERED
    assert article.source_id == feed.source_id
    assert article.feed_id == feed.feed_id


# --------------------------------------------------------------------------- AC3


def test_one_feed_raising_does_not_stop_the_others(app_session: Session) -> None:
    """AC3. A single flaky publisher must not freeze ingestion for everyone."""
    bad = _feed_with_source(app_session, url="https://feeds.example/bad.xml")
    good = make_feed(
        app_session, make_source(app_session, "Other"), url="https://feeds.example/good.xml"
    )
    app_session.commit()

    report = run_cycle(
        app_session,
        FakeFetcher({bad.url: RuntimeError("boom"), good.url: ONE_ITEM}),
        sleep=lambda _: None,
    )

    assert report.failed == 1
    assert report.discovered == 1
    assert len(articles(app_session)) == 1


def test_one_feed_timing_out_does_not_stop_the_others(app_session: Session) -> None:
    bad = _feed_with_source(app_session, url="https://feeds.example/bad.xml")
    good = make_feed(
        app_session, make_source(app_session, "Other"), url="https://feeds.example/good.xml"
    )
    app_session.commit()

    report = run_cycle(
        app_session,
        FakeFetcher(
            {
                bad.url: FetchResult(status=None, error="ReadTimeout: timed out"),
                good.url: ONE_ITEM,
            }
        ),
        sleep=lambda _: None,
    )

    assert (report.failed, report.discovered) == (1, 1)
    state = poll_state.get(app_session, bad.feed_id)
    assert state is not None
    assert state.last_status is None
    assert state.consecutive_failures == 1


def test_malformed_xml_does_not_stop_the_others(app_session: Session) -> None:
    bad = _feed_with_source(app_session, url="https://feeds.example/bad.xml")
    good = make_feed(
        app_session, make_source(app_session, "Other"), url="https://feeds.example/good.xml"
    )
    app_session.commit()

    report = run_cycle(
        app_session,
        FakeFetcher({bad.url: b"<html>404 Not Found</html>", good.url: ONE_ITEM}),
        sleep=lambda _: None,
    )

    assert (report.failed, report.discovered) == (1, 1)


def test_a_feed_that_raises_is_recorded_as_a_failure(app_session: Session) -> None:
    """⚠️ The rollback that contains a crashing feed also discards the poll-state write, so
    without a separate transaction the one failure class the except clause exists to survive is
    the one that leaves no trace. A feed raising on every poll for a week would keep reading
    ``last_status=200, consecutive_failures=0`` on the admin surface — reported as healthy.
    """
    feed = _feed_with_source(app_session)
    run_cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}), sleep=lambda _: None)

    for _ in range(3):
        run_cycle(
            app_session,
            FakeFetcher({feed.url: RuntimeError("boom")}),
            sleep=lambda _: None,
        )

    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert state.consecutive_failures == 3
    assert state.last_status is None
    assert state.last_error is not None and "RuntimeError" in state.last_error


def test_a_crashing_feed_does_not_lose_another_feeds_work(app_session: Session) -> None:
    """The recovery write must not itself become a way to lose the cycle."""
    bad = _feed_with_source(app_session, url="https://feeds.example/bad.xml")
    good = make_feed(
        app_session, make_source(app_session, "Other"), url="https://feeds.example/good.xml"
    )
    app_session.commit()

    run_cycle(
        app_session,
        FakeFetcher({bad.url: RuntimeError("boom"), good.url: ONE_ITEM}),
        sleep=lambda _: None,
    )

    assert len(articles(app_session)) == 1
    app_session.expire_all()
    assert poll_state.get(app_session, bad.feed_id) is not None


def test_a_feed_we_cannot_read_yet_is_counted_not_dropped(app_session: Session) -> None:
    """A sitemap source is registered correctly and simply not implemented here. Without a
    count it appears in no report, no log line and no poll-state row — indistinguishable from a
    publisher nobody added. One is on the live roster.
    """
    feed = _feed_with_source(app_session, discovery_method=DiscoveryMethod.SITEMAP)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    report = run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert fetcher.calls == []
    assert report.skipped_feeds == 1
    assert (report.polled, report.failed) == (0, 0)


def test_consecutive_failures_climb_and_reset(app_session: Session) -> None:
    """The count separates a rotted URL from an outage when read with last_status."""
    feed = _feed_with_source(app_session)
    failing = FakeFetcher({})  # unknown URL -> 404

    for _ in range(3):
        run_cycle(app_session, failing, sleep=lambda _: None)
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert (state.consecutive_failures, state.last_status) == (3, 404)

    run_cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}), sleep=lambda _: None)
    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert state.consecutive_failures == 0


# --------------------------------------------------------------------------- the gates


@pytest.mark.parametrize(
    ("feed_kw", "source_kw"),
    [
        ({"enabled": False}, {}),
        ({}, {"enabled": False}),
        ({}, {"permitted_to_ingest": False}),
    ],
    ids=["feed disabled", "publisher disabled", "publisher not permitted"],
)
def test_a_gated_feed_is_not_polled(
    app_session: Session, feed_kw: dict[str, Any], source_kw: dict[str, Any]
) -> None:
    """All three gates, never a subset — they are set by different people for different
    reasons, which is exactly when one gets forgotten at the call site.
    """
    feed = _feed_with_source(app_session, source=source_kw, **feed_kw)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    report = run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert fetcher.calls == []
    assert report.polled == 0
    assert articles(app_session) == []


def test_a_non_rss_feed_is_not_polled(app_session: Session) -> None:
    """Sitemap discovery is a later story; this one reads RSS and Atom."""
    feed = _feed_with_source(app_session, discovery_method=DiscoveryMethod.SITEMAP)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert fetcher.calls == []


# --------------------------------------------------------------------------- body vs teaser


def test_a_tier_three_feed_never_stores_a_body_however_fat_the_teaser(
    app_session: Session,
) -> None:
    """⚠️ The guard that keeps 'body_text is empty until extraction ships' true.

    A description is a teaser whatever its length. Storing one as a body makes every later
    stage read 120 characters believing it has an article — and the emptiness of this column
    is currently a clean signal that something depends on.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.EXTRACTION)
    fat = feed_xml(("g1", "One"), description="x" * 4000)

    run_cycle(app_session, FakeFetcher({feed.url: fat}), sleep=lambda _: None)

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.lede is not None and len(article.lede) == 4000


def test_a_tier_three_feed_shipping_content_encoded_still_stores_no_body(
    app_session: Session,
) -> None:
    """⚠️ The falsifier for the tier gate itself, which nothing else covers.

    The teaser test above passes with the gate deleted: its feed is description-only, so
    ``item.content`` is None either way and it verifies ``parse``'s teaser/body split rather
    than the gate. A tier-3 feed that ships ``<content:encoded>`` anyway is the case that
    separates them — and WordPress-based publishers ship it routinely.

    What rests on this: ``body_text`` being empty until tier-3 acquisition lands is the premise
    of the sprint-2 sequencing constraint. This gate is what keeps it true.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.EXTRACTION)
    raw = (
        b'<?xml version="1.0"?>'
        b'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        b"<channel><title>Ex</title><item><title>One</title>"
        b"<link>https://x.example/g1</link><guid>g1</guid>"
        b"<description>A teaser.</description>"
        b"<content:encoded><![CDATA[THE WHOLE ARTICLE]]></content:encoded>"
        b"</item></channel></rss>"
    )

    run_cycle(app_session, FakeFetcher({feed.url: raw}), sleep=lambda _: None)

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.lede == "A teaser."


def test_a_tier_one_feed_stores_the_content_element_as_the_body(
    app_session: Session,
) -> None:
    """The branch exists so that ``1_full_feed`` is a value the registry can act on. No
    publisher on the v1 roster ships one, which is why the tier is a human determination.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)
    raw = (
        b'<?xml version="1.0"?>'
        b'<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        b"<channel><title>Ex</title><item><title>One</title>"
        b"<link>https://x.example/g1</link><guid>g1</guid>"
        b"<description>A teaser.</description>"
        b"<content:encoded><![CDATA[THE WHOLE ARTICLE]]></content:encoded>"
        b"</item></channel></rss>"
    )

    run_cycle(app_session, FakeFetcher({feed.url: raw}), sleep=lambda _: None)

    article = articles(app_session)[0]
    assert article.body_text == "THE WHOLE ARTICLE"
    assert article.lede == "A teaser."


# --------------------------------------------------------------------------- politeness


def test_the_user_agent_carries_no_contact_url() -> None:
    """⚠️ Measured against a live publisher: appending "(+https://…)" turned a 200 in 0.8 s
    into a read timeout at 25 s, three times over. It fails as a hang, not a refusal.
    """
    assert "http" not in DEFAULT_USER_AGENT


def test_a_publisher_may_override_the_user_agent(app_session: Session) -> None:
    feed = _feed_with_source(app_session, source={"user_agent": "Custom/9"})
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert fetcher.calls[0][1] == "Custom/9"


def test_feeds_of_one_publisher_share_a_single_rate_budget(app_session: Session) -> None:
    """FR-I3 politeness is a promise about one host, so N feeds honouring it independently
    would exceed it N-fold. That is why the limit lives on the publisher.
    """
    source = make_source(app_session, rate_limit_per_min=60)
    a = make_feed(app_session, source, url="https://feeds.example/a.xml")
    b = make_feed(app_session, source, url="https://feeds.example/b.xml")
    app_session.commit()

    slept: list[float] = []
    run_cycle(
        app_session,
        FakeFetcher({a.url: ONE_ITEM, b.url: feed_xml(("g9", "Nine"))}),
        sleep=slept.append,
        clock=lambda: 0.0,
    )

    assert slept == [1.0]


def test_feeds_of_different_publishers_do_not_wait_on_each_other(
    app_session: Session,
) -> None:
    a = _feed_with_source(app_session, url="https://feeds.example/a.xml")
    b = make_feed(app_session, make_source(app_session, "Other"), url="https://feeds.example/b.xml")
    app_session.commit()

    slept: list[float] = []
    run_cycle(
        app_session,
        FakeFetcher({a.url: ONE_ITEM, b.url: feed_xml(("g9", "Nine"))}),
        sleep=slept.append,
        clock=lambda: 0.0,
    )

    assert slept == []


# --------------------------------------------------------------------------- conditional GET


def test_the_second_poll_carries_the_validator_the_publisher_gave_us(
    app_session: Session,
) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM}, etag='W/"abc"')

    run_cycle(app_session, fetcher, sleep=lambda _: None)
    run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert fetcher.calls[0][2] == {}
    assert fetcher.calls[1][2] == {"If-None-Match": 'W/"abc"'}


def test_a_not_modified_response_stores_nothing_and_is_not_a_failure(
    app_session: Session,
) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM}, etag='W/"abc"')

    run_cycle(app_session, fetcher, sleep=lambda _: None)
    report = run_cycle(app_session, fetcher, sleep=lambda _: None)

    assert (report.not_modified, report.discovered, report.failed) == (1, 0, 0)
    assert len(articles(app_session)) == 1


def test_a_not_modified_response_does_not_clear_the_stored_validator(
    app_session: Session,
) -> None:
    """⚠️ A 304 carries no body and frequently no ETag. Writing the absent value through would
    clear the validator that just produced the 304, so the saving would disappear after
    exactly one successful use and every later poll would transfer the whole feed again.
    """
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM}, etag='W/"abc"')

    for _ in range(3):
        run_cycle(app_session, fetcher, sleep=lambda _: None)

    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert state.etag == 'W/"abc"'
    assert fetcher.calls[-1][2] == {"If-None-Match": 'W/"abc"'}


# --------------------------------------------------------------- cycle cost (RFC §7.1)


def test_consecutive_requests_go_to_different_publishers(app_session: Session) -> None:
    """Politeness is per publisher, so polling one publisher's feeds back to back means waiting
    out the full gap between each — the worst order, and the one feed id order produces.
    """
    first = make_source(app_session, "First")
    second = make_source(app_session, "Second")
    urls = {}
    for source, tag in ((first, "a"), (second, "b")):
        for n in (1, 2):
            feed = make_feed(app_session, source, url=f"https://feeds.example/{tag}{n}.xml")
            urls[feed.url] = feed_xml((f"{tag}{n}", "T"))
    app_session.commit()

    fetcher = FakeFetcher(urls)
    run_cycle(app_session, fetcher, sleep=lambda _: None)

    order = [url.rsplit("/", 1)[1][0] for url, _, _ in fetcher.calls]
    assert order in (["a", "b", "a", "b"], ["b", "a", "b", "a"]), order


class SimClock:
    """A clock that moves only when work happens, never merely by being read.

    Reading a clock must be free, or the number of times the code under test happens to call it
    becomes part of the measurement — which makes the test assert the implementation rather than
    the behaviour.
    """

    def __init__(self, fetch_cost: float = 0.5) -> None:
        self.now = 0.0
        self.fetch_cost = fetch_cost
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def spend_on_fetch(self) -> None:
        self.now += self.fetch_cost


def test_interleaving_spends_less_of_the_freshness_budget_on_waiting(
    app_session: Session,
) -> None:
    """The cycle sits *inside* the freshness budget: an article waits the interval plus its
    feed's position in the cycle. Same politeness, less waiting.

    Two publishers, two feeds each, at one request per minute — so a publisher may be polled
    once a minute and there are four feeds to get through.

    Feed id order is ``a1 a2 b1 b2``: two full 60 s waits, ~120 s of wall time. Interleaved it
    is ``a1 b1 a2 b2``, and the whole cycle waits **once** — by the time the second publisher's
    turn comes round its gap has already elapsed during the first publisher's wait. ~61 s, for
    exactly the same politeness: each publisher still sees a minute between its requests.
    """
    urls = {}
    for name, tag in (("First", "a"), ("Second", "b")):
        source = make_source(app_session, name, rate_limit_per_min=1)
        for n in (1, 2):
            feed = make_feed(app_session, source, url=f"https://feeds.example/{tag}{n}.xml")
            urls[feed.url] = feed_xml((f"{tag}{n}", "T"))
    app_session.commit()

    clock = SimClock()
    fetcher = FakeFetcher(urls, on_call=clock.spend_on_fetch)

    run_cycle(app_session, fetcher, sleep=clock.sleep, clock=clock)

    # The property is the wall time, not the number of sleeps: feed id order costs two full
    # gaps here and interleaving costs one, and it is the total that lands in the budget.
    assert clock.now < 120, f"cycle took {clock.now}s; feed id order would cost ~120s"
    assert sum(clock.slept) < 60, clock.slept
    # Politeness is unchanged — each publisher was still polled at most once per gap.
    assert len(fetcher.calls) == 4


def test_the_cycle_reports_how_long_it_took(app_session: Session) -> None:
    """Reported because it is a term in the freshness budget and not a constant — it grows with
    the feed count and with how politely each publisher is polled.
    """
    feed = _feed_with_source(app_session)
    clock = SimClock(fetch_cost=30.0)

    report = run_cycle(
        app_session,
        FakeFetcher({feed.url: ONE_ITEM}, on_call=clock.spend_on_fetch),
        sleep=clock.sleep,
        clock=clock,
    )

    assert report.duration_seconds == 30.0


def test_no_transaction_is_held_open_across_the_fetch(app_session: Session) -> None:
    """⚠️ A transaction held open across a network fetch pins the database's oldest xmin, so
    VACUUM cannot reclaim dead tuples anywhere in the database until the slowest publisher
    answers — on a volume that cannot be expanded.
    """
    _feed_with_source(app_session)
    open_during_fetch: list[bool] = []

    def watching_fetcher(url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        open_during_fetch.append(app_session.in_transaction())
        return FetchResult(status=200, body=ONE_ITEM)

    run_cycle(app_session, watching_fetcher, sleep=lambda _: None)

    assert open_during_fetch == [False]
