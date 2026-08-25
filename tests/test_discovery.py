"""One discovery cycle (FR-I1) — MER-16's acceptance criteria.

No network: ``run_cycle`` takes its fetcher as an argument, so these drive the real cycle
against feeds we author here.
"""

from collections.abc import Mapping
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
    ) -> None:
        self._bodies = bodies
        self._etag = etag
        self.calls: list[tuple[str, str, Mapping[str, str]]] = []

    def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
        self.calls.append((url, user_agent, dict(headers)))
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
