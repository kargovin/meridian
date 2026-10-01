"""One discovery cycle (FR-I1) — MER-16's acceptance criteria.

No network: ``run_cycle`` takes its fetcher as an argument, so these drive the real cycle
against feeds we author here.
"""

import datetime as dt
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import (
    AcquisitionTier,
    BodyProvenance,
    DiscoveryMethod,
    PipelineState,
    RightsLevel,
    Stage,
)
from sqlalchemy.orm import Session

from meridian.db import poll_state, sources, work_queue
from meridian.db.models import (
    AlternateCopy,
    CanonicalRecord,
    Feed,
    FeedPollState,
    PipelineWork,
    Source,
)
from meridian.ingest.acquire import run_batch
from meridian.ingest.discovery import CycleReport, run_cycle
from meridian.ingest.fetch import DEFAULT_USER_AGENT, Fetcher, FetchResult
from meridian.ingest.network import Network
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache
from tests.factories import (
    make_alternate_copy,
    make_article,
    make_feed,
    make_source,
)

pytestmark = pytest.mark.postgres

FIXTURES = Path(__file__).parent / "fixtures"


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

    @property
    def feeds(self) -> list[tuple[str, str, Mapping[str, str]]]:
        """The feed requests alone. A cycle's first request to a host is its ``robots.txt``,
        which for an unknown URL this fake answers 404 — open, per RFC 9309."""
        return [call for call in self.calls if not call[0].endswith("/robots.txt")]


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


def _cycle(session: Session, fetcher: Fetcher, *, clock: SimClock | None = None) -> CycleReport:
    """One cycle over a fresh process-wide ``Network`` — the shape ``__main__`` builds."""
    clock = clock or SimClock(fetch_cost=0.0)
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    network = Network(fetcher=fetcher, robots=RobotsCache(fetcher, pacer, clock=clock), pacer=pacer)
    return run_cycle(session, network, clock=clock)


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

    first = _cycle(app_session, fetcher)
    assert (first.discovered, first.failed) == (3, 0)

    for _ in range(19):
        again = _cycle(app_session, fetcher)
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
        report = _cycle(app_session, fetcher)
        assert report.failed == 0

    work = app_session.query(PipelineWork).all()
    assert len(work) == 1
    assert work[0].stage is Stage.ACQUIRE


def test_a_new_item_appearing_later_is_the_only_one_stored(app_session: Session) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})
    _cycle(app_session, fetcher)

    fetcher = FakeFetcher({feed.url: THREE_ITEMS})
    report = _cycle(app_session, fetcher)

    assert report.discovered == 2
    assert len(articles(app_session)) == 3


def test_a_discovered_record_carries_what_the_feed_said(app_session: Session) -> None:
    feed = _feed_with_source(app_session)
    _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

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

    report = _cycle(
        app_session,
        FakeFetcher({bad.url: RuntimeError("boom"), good.url: ONE_ITEM}),
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

    report = _cycle(
        app_session,
        FakeFetcher(
            {
                bad.url: FetchResult(status=None, error="ReadTimeout: timed out"),
                good.url: ONE_ITEM,
            }
        ),
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

    report = _cycle(
        app_session,
        FakeFetcher({bad.url: b"<html>404 Not Found</html>", good.url: ONE_ITEM}),
    )

    assert (report.failed, report.discovered) == (1, 1)


def test_a_feed_that_raises_is_recorded_as_a_failure(app_session: Session) -> None:
    """⚠️ The rollback that contains a crashing feed also discards the poll-state write, so
    without a separate transaction the one failure class the except clause exists to survive is
    the one that leaves no trace. A feed raising on every poll for a week would keep reading
    ``last_status=200, consecutive_failures=0`` on the admin surface — reported as healthy.
    """
    feed = _feed_with_source(app_session)
    _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    for _ in range(3):
        _cycle(
            app_session,
            FakeFetcher({feed.url: RuntimeError("boom")}),
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

    _cycle(
        app_session,
        FakeFetcher({bad.url: RuntimeError("boom"), good.url: ONE_ITEM}),
    )

    assert len(articles(app_session)) == 1
    app_session.expire_all()
    assert poll_state.get(app_session, bad.feed_id) is not None


@pytest.mark.parametrize("method", [DiscoveryMethod.WEBSUB, DiscoveryMethod.SECTION_SCRAPE])
def test_a_feed_we_cannot_read_yet_is_counted_not_dropped(
    app_session: Session, method: DiscoveryMethod
) -> None:
    """A WebSub or section-scrape feed is registered correctly and simply not implemented here.
    Without a count it appears in no report, no log line and no poll-state row —
    indistinguishable from a publisher nobody added.
    """
    feed = _feed_with_source(app_session, discovery_method=method)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    report = _cycle(app_session, fetcher)

    assert fetcher.calls == []
    assert report.skipped_feeds == 1
    assert (report.polled, report.failed) == (0, 0)


def test_consecutive_failures_climb_and_reset(app_session: Session) -> None:
    """The count separates a rotted URL from an outage when read with last_status."""
    feed = _feed_with_source(app_session)
    failing = FakeFetcher({})  # unknown URL -> 404

    for _ in range(3):
        _cycle(app_session, failing)
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert (state.consecutive_failures, state.last_status) == (3, 404)

    _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))
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

    report = _cycle(app_session, fetcher)

    assert fetcher.calls == []
    assert report.polled == 0
    assert articles(app_session) == []


@pytest.mark.parametrize("method", [DiscoveryMethod.WEBSUB, DiscoveryMethod.SECTION_SCRAPE])
def test_a_feed_of_an_unimplemented_method_is_not_polled(
    app_session: Session, method: DiscoveryMethod
) -> None:
    feed = _feed_with_source(app_session, discovery_method=method)
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    _cycle(app_session, fetcher)

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

    _cycle(app_session, FakeFetcher({feed.url: fat}))

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

    _cycle(app_session, FakeFetcher({feed.url: raw}))

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.lede == "A teaser."


def test_a_tier_one_feed_stores_the_content_element_as_the_body(
    app_session: Session,
) -> None:
    """The branch exists so that ``1_full_feed`` is a value the registry can act on. Eight
    v1-roster feeds carry the body and four of those publishers grant the rights to hold it;
    no mainstream publisher does either, which is why the tier is a human determination and
    not an inference from the feed.
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

    _cycle(app_session, FakeFetcher({feed.url: raw}))

    article = articles(app_session)[0]
    assert article.body_text == "THE WHOLE ARTICLE"
    assert article.lede == "A teaser."
    # ⚠️ Provenance records an *event*, and this is the only code that witnesses it. Written
    # here rather than by a later stage because a body stored with NULL provenance is
    # indistinguishable downstream from one nobody obtained (RFC §5.1).
    assert article.body_provenance is BodyProvenance.TIER1_FEED


#: A real tier-1 body: Global Voices' ``<content:encoded>`` for
#: https://globalvoices.org/2026/09/25/movement-from-behind-your-gadget/, as the dev stack's
#: poll stored it in late September 2026, trimmed to 9 of its blocks. Kept real because
#: what a CMS emits — a caption ``div``, italics nested round links, a byline built from
#: spans — is what a hand-written fixture leaves out.
GLOBAL_VOICES_BODY = (FIXTURES / "global_voices_content_encoded.html").read_text()

TAG = re.compile(r"</?[a-zA-Z][^>]*>")


def one_item_with_content(content: str) -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>Ex</title><item><title>One</title>"
        "<link>https://x.example/g1</link><guid>g1</guid>"
        "<description>A teaser.</description>"
        f"<content:encoded><![CDATA[{content}]]></content:encoded>"
        "</item></channel></rss>"
    ).encode()


def test_a_tier_one_body_is_stored_as_text_not_the_feeds_html(app_session: Session) -> None:
    """The body is what ``content_hash`` and the SimHash fingerprint read, and every stage
    after them. Stored as the feed's HTML, both hashed tags and URLs, and a tier-1 copy of an
    article could never hash equal to the same text from any other tier.

    ⚠️ The earlier tier-1 tests used plain-text fixtures, which pass whether or not anything
    converts the markup. This one is real publisher HTML.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)

    _cycle(app_session, FakeFetcher({feed.url: one_item_with_content(GLOBAL_VOICES_BODY)}))

    article = articles(app_session)[0]
    assert article.body_text is not None
    assert TAG.search(article.body_text) is None
    lines = article.body_text.split("\n")
    assert lines[0] == "Online vs offline protest, how effective it is?"
    assert lines[5] == "Both of them are effective, no?"
    assert lines[-1] == "Written by Juliana Harsianti"
    assert len(lines) == 9
    assert article.body_provenance is BodyProvenance.TIER1_FEED


def test_a_plain_text_body_is_not_read_as_html(app_session: Session) -> None:
    """An Atom ``<content type="text">`` arrives unescaped; run through the HTML converter,
    "<b and c>" would be taken for a tag and deleted."""
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)
    atom = (
        b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Ex</title>'
        b'<entry><id>g1</id><title>One</title><link href="https://x.example/g1"/>'
        b'<content type="text">Para one.\n\nPara two: a&lt;b and c&gt;d, AT&amp;T.</content>'
        b"</entry></feed>"
    )

    _cycle(app_session, FakeFetcher({feed.url: atom}))

    article = articles(app_session)[0]
    assert article.body_text == "Para one.\nPara two: a<b and c>d, AT&T."
    assert article.body_provenance is BodyProvenance.TIER1_FEED


def test_a_content_element_holding_only_markup_is_no_body(app_session: Session) -> None:
    """An image and nothing else is not an article. Storing an empty string would be a body
    downstream — hashed, fingerprinted, handed to a summarizer — with provenance claiming the
    feed shipped it.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)

    _cycle(
        app_session,
        FakeFetcher(
            {feed.url: one_item_with_content('<div><img src="https://x.example/a.jpg"/></div>')}
        ),
    )

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.body_provenance is None


def content_xml(*items: tuple[str, str]) -> bytes:
    """A feed whose items carry the whole article in ``<content:encoded>``."""
    entries = "".join(
        f"<item><title>{title}</title><link>https://x.example/{guid}</link>"
        f"<guid>{guid}</guid><description>A teaser.</description>"
        f"<content:encoded><![CDATA[THE WHOLE ARTICLE {guid}]]></content:encoded></item>"
        for guid, title in items
    )
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>Ex</title>{entries}</channel></rss>"
    ).encode()


CONTENT_ITEM = content_xml(("g1", "One"))


def test_a_headline_only_publisher_gets_no_body_even_from_a_full_feed(
    app_session: Session,
) -> None:
    """A feed that ships the article is not a licence to hold it.

    Four v1-roster publishers ship full text in a feed their terms forbid us to use. The tier
    describes the feed; the rights say whether the body may be held; and this is the one cell
    of that table where the two disagree. Before this gate the file kept the two from meeting
    by mis-registering those feeds as ``3_extraction``, and one admin edit undid it.
    """
    feed = _feed_with_source(
        app_session,
        acquisition_tier=AcquisitionTier.FULL_FEED,
        source={"rights_level": RightsLevel.HEADLINE_ONLY},
    )

    _cycle(app_session, FakeFetcher({feed.url: CONTENT_ITEM}))

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.body_provenance is None
    # The record itself is still made: headline-only publishers feed classification and
    # clustering (FR-S5). What is withheld is the body, not the article.
    assert article.title == "One"
    assert article.lede == "A teaser."


def test_revoking_rights_stops_new_bodies_and_keeps_the_ones_held(
    app_session: Session,
) -> None:
    """A downgrade takes effect on the next poll with no tier edit and no cascade.

    Both halves are the point. New bodies stop — the gate reads the registry, not a copy taken
    when the feed was registered. And the body already held stays: stopping collection and
    unpublishing are different acts (Legal A-L5), and the second is a workflow of its own.
    A gate that deleted on downgrade would be doing the second act by accident.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.FULL_FEED)
    _cycle(app_session, FakeFetcher({feed.url: content_xml(("g1", "One"))}))
    assert articles(app_session)[0].body_text == "THE WHOLE ARTICLE g1"

    source = sources.get(app_session, feed.source_id)
    assert source is not None
    assert (
        sources.set_rights_level(
            app_session,
            source.source_id,
            level=RightsLevel.HEADLINE_ONLY,
            expected_updated_at=source.updated_at,
        )
        is not None
    )
    app_session.commit()

    _cycle(
        app_session,
        FakeFetcher({feed.url: content_xml(("g1", "One"), ("g2", "Two"))}),
    )

    first, second = articles(app_session)
    assert (first.guid, first.body_text) == ("g1", "THE WHOLE ARTICLE g1")
    assert (second.guid, second.body_text, second.body_provenance) == ("g2", None, None)


def test_a_record_with_no_body_has_no_provenance(app_session: Session) -> None:
    """The other half: provenance is not a default, it is a statement about where a body came
    from. Every record on the current roster takes this branch.
    """
    feed = _feed_with_source(app_session, acquisition_tier=AcquisitionTier.EXTRACTION)

    _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    article = articles(app_session)[0]
    assert article.body_text is None
    assert article.body_provenance is None


# --------------------------------------------------------------------------- robots.txt

FEEDS_ROBOTS = "https://feeds.example/robots.txt"


def test_a_feed_robots_disallows_is_not_polled_and_the_poll_state_says_why(
    app_session: Session,
) -> None:
    """MER-23 AC8. The roster case is AP: ``Disallow: /*.rss`` while articles stay allowed. The
    feed is recorded as failed with the reason — not skipped in silence, which on the admin
    surface would look exactly like a feed nobody added."""
    feed = _feed_with_source(app_session, url="https://feeds.example/feeds/a.xml")
    fetcher = FakeFetcher(
        {
            FEEDS_ROBOTS: FetchResult(status=200, body=b"User-agent: *\nDisallow: /feeds/\n"),
            feed.url: ONE_ITEM,
        }
    )

    report = _cycle(app_session, fetcher)

    assert fetcher.feeds == []
    assert (report.robots_blocked, report.polled, report.failed) == (1, 0, 0)
    assert articles(app_session) == []
    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert state.consecutive_failures == 1
    assert state.last_status is None
    assert state.last_error is not None and "robots" in state.last_error


def test_robots_is_read_once_per_host_not_once_per_feed(app_session: Session) -> None:
    source = make_source(app_session)
    for name in ("a", "b", "c"):
        make_feed(app_session, source, url=f"https://feeds.example/{name}.xml")
    app_session.commit()
    fetcher = FakeFetcher(
        {FEEDS_ROBOTS: FetchResult(status=200, body=b"User-agent: *\nAllow: /\n")}
    )

    _cycle(app_session, fetcher)

    assert [url for url, _, _ in fetcher.calls].count(FEEDS_ROBOTS) == 1
    assert len(fetcher.feeds) == 3


def test_an_unreachable_robots_txt_closes_the_host_for_this_cycle(app_session: Session) -> None:
    """RFC 9309: a 5xx on robots.txt with no cached copy means fetch nothing. Failing open on an
    outage is the mistake the spike found in the scraper we chose not to use."""
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher(
        {FEEDS_ROBOTS: FetchResult(status=503, error="Service Unavailable"), feed.url: ONE_ITEM}
    )

    report = _cycle(app_session, fetcher)

    assert fetcher.feeds == []
    assert report.robots_blocked == 1


# --------------------------------------------------------------------------- politeness


def test_the_user_agent_carries_no_contact_url() -> None:
    """⚠️ Measured against a live publisher: appending "(+https://…)" turned a 200 in 0.8 s
    into a read timeout at 25 s, three times over. It fails as a hang, not a refusal.
    """
    assert "http" not in DEFAULT_USER_AGENT


def test_a_publisher_may_override_the_user_agent(app_session: Session) -> None:
    feed = _feed_with_source(app_session, source={"user_agent": "Custom/9"})
    fetcher = FakeFetcher({feed.url: ONE_ITEM})

    _cycle(app_session, fetcher)

    assert fetcher.feeds[0][1] == "Custom/9"


def test_feeds_of_one_publisher_share_a_single_rate_budget(app_session: Session) -> None:
    """FR-I3 politeness is a promise about one host, so N feeds honouring it independently
    would exceed it N-fold. That is why the limit lives on the publisher.
    """
    source = make_source(app_session, rate_limit_per_min=60)
    a = make_feed(app_session, source, url="https://feeds.example/a.xml")
    b = make_feed(app_session, source, url="https://feeds.example/b.xml")
    app_session.commit()

    clock = SimClock(fetch_cost=0.0)
    _cycle(
        app_session, FakeFetcher({a.url: ONE_ITEM, b.url: feed_xml(("g9", "Nine"))}), clock=clock
    )

    # Three requests to one publisher — its robots.txt, then each feed — one gap apart.
    assert clock.slept == [1.0, 1.0]


def test_feeds_of_different_publishers_do_not_wait_on_each_other(
    app_session: Session,
) -> None:
    a = _feed_with_source(app_session, url="https://feeds.example/a.xml")
    b = make_feed(app_session, make_source(app_session, "Other"), url="https://feeds.example/b.xml")
    app_session.commit()

    clock = SimClock(fetch_cost=0.0)
    _cycle(
        app_session, FakeFetcher({a.url: ONE_ITEM, b.url: feed_xml(("g9", "Nine"))}), clock=clock
    )

    # Both feeds share a host, so one robots.txt fetch serves both; it was the first
    # publisher's request and only the first publisher waits out a gap after it. The second
    # publisher's first request is its feed — a pacer keyed by host would make it wait too.
    assert clock.slept == [2.0]


# --------------------------------------------------------------------------- conditional GET


def test_the_second_poll_carries_the_validator_the_publisher_gave_us(
    app_session: Session,
) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM}, etag='W/"abc"')

    _cycle(app_session, fetcher)
    _cycle(app_session, fetcher)

    assert fetcher.feeds[0][2] == {}
    assert fetcher.feeds[1][2] == {"If-None-Match": 'W/"abc"'}


def test_a_not_modified_response_stores_nothing_and_is_not_a_failure(
    app_session: Session,
) -> None:
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher({feed.url: ONE_ITEM}, etag='W/"abc"')

    _cycle(app_session, fetcher)
    report = _cycle(app_session, fetcher)

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
        _cycle(app_session, fetcher)

    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert state.etag == 'W/"abc"'
    assert fetcher.feeds[-1][2] == {"If-None-Match": 'W/"abc"'}


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
    _cycle(app_session, fetcher)

    order = [url.rsplit("/", 1)[1][0] for url, _, _ in fetcher.feeds]
    assert order in (["a", "b", "a", "b"], ["b", "a", "b", "a"]), order


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

    _cycle(app_session, fetcher, clock=clock)

    # The property is the wall time, not the number of sleeps: feed id order costs two full
    # gaps here and interleaving costs one, and it is the total that lands in the budget.
    # Plus one robots.txt fetch for the host, which takes the first publisher's first slot in
    # either order: ~121 s interleaved against ~181 s in feed order.
    assert clock.now < 180, f"cycle took {clock.now}s; feed id order would cost ~181s"
    assert sum(clock.slept) < 120, clock.slept
    # Politeness is unchanged — each publisher was still polled at most once per gap.
    assert len(fetcher.feeds) == 4


def test_the_cycle_reports_how_long_it_took(app_session: Session) -> None:
    """Reported because it is a term in the freshness budget and not a constant — it grows with
    the feed count and with how politely each publisher is polled.
    """
    feed = _feed_with_source(app_session)
    clock = SimClock(fetch_cost=30.0)

    report = _cycle(
        app_session, FakeFetcher({feed.url: ONE_ITEM}, on_call=clock.spend_on_fetch), clock=clock
    )

    # Two requests on a cold cache: the host's robots.txt, then the feed.
    assert report.duration_seconds == 60.0


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

    _cycle(app_session, watching_fetcher)

    # Both requests — robots.txt and the feed — happen with no transaction open.
    assert open_during_fetch == [False, False]


# --------------------------------------------------------------- url canonicalisation


def _item_without_guid(link: str) -> bytes:
    """One article, no ``<guid>`` — so its identity is its link, as the RSS spec suggests."""
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Ex</title>'
        f"<item><title>Floods displace thousands</title><link>{link}</link>"
        "<description>A teaser.</description></item></channel></rss>"
    ).encode()


def test_one_article_reached_by_two_feeds_is_stored_once(app_session: Session) -> None:
    """⚠️ The defect this canonicalisation exists for, and it is on the live roster.

    A publisher tags each feed's links with its own tracking parameter — one source decorates
    every link with ``?traffic_source=``. Two section feeds then carry the same article under
    two URLs, and with no ``<guid>`` the link *is* the identity, so both unique constraints see
    two different articles. One story, two canonical records, and the ≥2-distinct-source rule
    that gates summarization counts a publisher agreeing with itself.

    It cannot be repaired downstream: the second row exists the moment it is inserted, so the
    reduction has to happen before the write.
    """
    source = make_source(app_session)
    world = make_feed(app_session, source, url="https://feeds.example/world.xml")
    top = make_feed(app_session, source, url="https://feeds.example/top.xml")
    app_session.commit()

    _cycle(
        app_session,
        FakeFetcher(
            {
                world.url: _item_without_guid("https://x.example/floods?traffic_source=world"),
                top.url: _item_without_guid("https://x.example/floods?traffic_source=top"),
            }
        ),
    )

    stored = articles(app_session)
    assert len(stored) == 1, [a.url_canonical for a in stored]
    assert stored[0].url_canonical == "https://x.example/floods"


def test_the_stored_url_is_canonical_not_what_the_feed_wrote(app_session: Session) -> None:
    feed = _feed_with_source(app_session)
    raw = _item_without_guid("https://X.Example/floods?utm_source=rss&b=2&a=1#top")

    _cycle(app_session, FakeFetcher({feed.url: raw}))

    assert articles(app_session)[0].url_canonical == "https://x.example/floods?a=1&b=2"


# ----------------------------------------------------- the per-feed guard survives its own rollback


def test_a_feed_deleted_during_its_own_crashing_poll_does_not_stop_the_cycle(
    app_session: Session,
) -> None:
    """An operator deletes a rotted feed while its poll is in flight. The feed answers, and
    the article insert then violates the foreign key — inside the transaction, so the guard's
    rollback is real and expires the instance. A log line reading ``feed.feed_id`` inside the
    except clause would then re-query a row that is gone and raise from inside the clause that
    exists to contain failures, and the rest of the roster would not be polled. Identifiers
    are copied before the try. (A crash *at* the fetch does not reach this: no transaction is
    open there and the rollback expires nothing.)"""
    a = _feed_with_source(app_session, url="https://a.example/feed.xml")
    b = _feed_with_source(app_session, url="https://b.example/feed.xml")
    doomed_id, doomed_url = a.feed_id, a.url

    def delete_the_feed() -> None:
        with Session(app_session.get_bind()) as db:
            db.execute(sa.delete(Feed).where(Feed.feed_id == doomed_id))
            db.commit()

    class Fetcher(FakeFetcher):
        def __call__(self, url: str, *, user_agent: str, headers: Mapping[str, str]) -> FetchResult:
            if url == doomed_url:
                delete_the_feed()
            return super().__call__(url, user_agent=user_agent, headers=headers)

    report = _cycle(app_session, Fetcher({doomed_url: ONE_ITEM, b.url: ONE_ITEM}))

    assert (report.polled, report.failed, report.discovered) == (2, 1, 1)


def test_an_unreachable_robots_txt_is_recorded_as_an_outage_not_a_rule(
    app_session: Session,
) -> None:
    """Fail-closed is right; the poll state must not say the publisher wrote a rule against
    us when its host was down for ten minutes. The two read differently on the admin surface
    and call for different responses from a person."""
    feed = _feed_with_source(app_session)
    fetcher = FakeFetcher(
        {FEEDS_ROBOTS: FetchResult(status=503, error="Service Unavailable"), feed.url: ONE_ITEM}
    )

    report = _cycle(app_session, fetcher)

    assert report.robots_blocked == 1
    state = app_session.get(FeedPollState, feed.feed_id)
    assert state is not None and state.last_error is not None
    assert "unreachable" in state.last_error and "disallowed" not in state.last_error


# --------------------------------------------------------------- a copy dedup already collapsed


def _representative(session: Session) -> CanonicalRecord:
    """A record another publisher ran first — what a collapsed copy is a note on."""
    first = make_source(session, "First Publisher")
    return make_article(session, first, guid="rep", url="https://first.example/rep")


def test_a_collapsed_copy_is_not_rediscovered_on_the_next_poll(app_session: Session) -> None:
    """⚠️ The loop this lookup exists to stop, driven through the real collapse.

    Collapsing deletes the record, so ``UNIQUE(source_id, guid)`` and ``UNIQUE(url_canonical)``
    no longer see it. Without the lookup every poll that still carries the item re-inserts it,
    re-fetches its body and re-collapses it, until the note's own unique constraint raises.
    """
    feed = _feed_with_source(app_session)
    representative = _representative(app_session)
    app_session.commit()
    fetcher = FakeFetcher({feed.url: ONE_ITEM})
    _cycle(app_session, fetcher)
    (arrival,) = [a for a in articles(app_session) if a.guid == "g1"]
    work = app_session.scalars(
        sa.select(PipelineWork).where(PipelineWork.article_id == arrival.article_id)
    ).one()
    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    report = _cycle(app_session, fetcher)

    assert report.discovered == 0
    assert [a.guid for a in articles(app_session)] == ["rep"]
    assert app_session.scalars(sa.select(PipelineWork)).all() == []
    assert len(app_session.scalars(sa.select(AlternateCopy)).all()) == 1


def test_a_collapsed_copy_is_recognised_by_its_guid_alone(app_session: Session) -> None:
    """The publisher moved the article to a new URL; its guid is what still says it is ours."""
    source = make_source(app_session)
    feed = make_feed(app_session, source, url="https://feeds.example/a.xml")
    make_alternate_copy(
        app_session, _representative(app_session), source, guid="g1", url="https://x.example/old"
    )
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    assert report.discovered == 0
    assert [a.guid for a in articles(app_session)] == ["rep"]


def test_a_collapsed_copy_is_recognised_by_its_url_alone(app_session: Session) -> None:
    """A note with no guid — here another publisher's — still holds its URL, as
    ``UNIQUE(url_canonical)`` would across every publisher."""
    feed = _feed_with_source(app_session)
    elsewhere = make_source(app_session, "Syndicator")
    make_alternate_copy(
        app_session, _representative(app_session), elsewhere, guid=None, url="https://x.example/g1"
    )
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    assert report.discovered == 0
    assert [a.guid for a in articles(app_session)] == ["rep"]


def test_another_publishers_note_with_the_same_guid_does_not_hide_an_item(
    app_session: Session,
) -> None:
    """A guid is an identity only within its publisher — two feeds can both say ``g1``."""
    feed = _feed_with_source(app_session)
    elsewhere = make_source(app_session, "Syndicator")
    make_alternate_copy(
        app_session,
        _representative(app_session),
        elsewhere,
        guid="g1",
        url="https://syndicator.example/g1",
    )
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    assert report.discovered == 1
    assert sorted(a.guid for a in articles(app_session)) == ["g1", "rep"]


# --------------------------------------------------------------------------- news sitemaps

SITE = "https://x.example"
NEWS_NS = (
    'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
    'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"'
)


def news_sitemap(*entries: tuple[str, str]) -> bytes:
    """A news sitemap listing ``(url, title)`` pairs."""
    urls = "".join(
        f"<url><loc>{url}</loc><news:news>"
        "<news:publication_date>2026-10-01T07:50:02-04:00</news:publication_date>"
        f"<news:title>{title}</news:title></news:news></url>"
        for url, title in entries
    )
    return f'<?xml version="1.0"?><urlset {NEWS_NS}>{urls}</urlset>'.encode()


def rss_item_with_body(guid: str = "g1", *, link: str = f"{SITE}/g1") -> bytes:
    return (
        '<?xml version="1.0"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        "<channel><title>Ex</title><item><title>Floods displace thousands</title>"
        f"<link>{link}</link><guid>{guid}</guid>"
        "<pubDate>Tue, 25 Aug 2026 09:14:02 GMT</pubDate>"
        "<description>A teaser.</description>"
        "<content:encoded><![CDATA[<p>THE WHOLE ARTICLE</p>]]></content:encoded>"
        "</item></channel></rss>"
    ).encode()


EMPTY_RSS = feed_xml()
# A headline of its own: the sitemap and the feed spell one article's title differently, which
# is what makes the record's title say which route it was made from.
G1_SITEMAP = news_sitemap((f"{SITE}/g1", "Thousands displaced as floods hit"))


def _publisher_with_feed_and_sitemap(
    session: Session, *, sitemap_first: bool = True, **source_kw: Any
) -> tuple[Feed, Feed]:
    """One publisher with an RSS feed and a news sitemap. The sitemap gets the lower id by
    default, so feed-id order alone would poll it first."""
    source = make_source(session, home_url=SITE, **source_kw)

    def sitemap() -> Feed:
        return make_feed(
            session,
            source,
            url="https://sitemaps.example/news.xml",
            discovery_method=DiscoveryMethod.SITEMAP,
            acquisition_tier=AcquisitionTier.EXTRACTION,
        )

    def rss() -> Feed:
        return make_feed(session, source, url="https://feeds.example/a.xml")

    if sitemap_first:
        sm, feed = sitemap(), rss()
    else:
        feed, sm = rss(), sitemap()
    session.commit()
    return feed, sm


def _sitemap_feed(session: Session, url: str = "https://sitemaps.example/news.xml") -> Feed:
    return _feed_with_source(
        session,
        url=url,
        discovery_method=DiscoveryMethod.SITEMAP,
        acquisition_tier=AcquisitionTier.EXTRACTION,
        source={"home_url": SITE},
    )


def test_a_sitemap_feed_is_polled_and_produces_records_and_work(app_session: Session) -> None:
    """AC1. One record and one acquire row per entry, created in the feed's transaction exactly
    as an RSS item's are; and the feed is no longer counted as unreadable."""
    feed = _sitemap_feed(app_session)
    raw = news_sitemap((f"{SITE}/a", "One"), (f"{SITE}/b", "Two"))

    report = _cycle(app_session, FakeFetcher({feed.url: raw}))

    assert (report.polled, report.discovered, report.skipped_feeds) == (1, 2, 0)
    stored = articles(app_session)
    assert [(a.url_canonical, a.guid, a.title) for a in stored] == [
        (f"{SITE}/a", f"{SITE}/a", "One"),
        (f"{SITE}/b", f"{SITE}/b", "Two"),
    ]
    assert all(a.lede is None and a.body_text is None for a in stored)
    assert all(a.feed_id == feed.feed_id for a in stored)
    work = app_session.scalars(sa.select(PipelineWork)).all()
    assert {w.article_id for w in work} == {a.article_id for a in stored}
    assert len(work) == len(stored)
    assert {w.stage for w in work} == {Stage.ACQUIRE}


def test_repolling_a_sitemap_creates_each_entry_once(app_session: Session) -> None:
    feed = _sitemap_feed(app_session)
    fetcher = FakeFetcher({feed.url: G1_SITEMAP})

    reports = [_cycle(app_session, fetcher) for _ in range(3)]

    assert [r.discovered for r in reports] == [1, 0, 0]
    assert [r.adopted for r in reports] == [0, 0, 0]
    assert len(articles(app_session)) == 1
    assert app_session.scalar(sa.select(sa.func.count()).select_from(PipelineWork)) == 1


def test_a_sitemap_entry_on_another_site_is_not_stored_and_is_counted(
    app_session: Session,
) -> None:
    """NPR's news sitemap lists member stations' articles on the stations' own sites. Stored
    under NPR, a station's article would be held on NPR's rights determination."""
    feed = _sitemap_feed(app_session)
    raw = news_sitemap(
        (f"{SITE}/ours", "Ours"),
        ("https://sub.x.example/also-ours", "Also ours"),
        ("https://www.station.example/theirs", "Theirs"),
    )

    report = _cycle(app_session, FakeFetcher({feed.url: raw}))

    assert sorted(a.url_canonical for a in articles(app_session)) == [
        "https://sub.x.example/also-ours",
        f"{SITE}/ours",
    ]
    assert (report.discovered, report.off_site_items, report.skipped_items) == (2, 1, 0)


def test_an_rss_item_on_another_site_is_still_stored(app_session: Session) -> None:
    """The site filter is the sitemap's alone: a feed is the publisher's own selection."""
    feed = _feed_with_source(app_session, source={"home_url": "https://elsewhere.example"})

    report = _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    assert (report.discovered, report.off_site_items) == (1, 0)


def test_the_npr_capture_keeps_npr_org_and_drops_the_station(app_session: Session) -> None:
    """AC2 end to end, on the real capture: served from googlecrawl.npr.org, read against the
    registry's home_url."""
    feed = _feed_with_source(
        app_session,
        url="https://googlecrawl.npr.org/news/sitemap_news.xml",
        discovery_method=DiscoveryMethod.SITEMAP,
        acquisition_tier=AcquisitionTier.EXTRACTION,
        source={"home_url": "https://www.npr.org"},
    )
    raw = (FIXTURES / "npr_sitemap_news.xml").read_bytes()

    report = _cycle(app_session, FakeFetcher({feed.url: raw}))

    assert (report.discovered, report.off_site_items) == (4, 1)
    assert all(a.url_canonical.startswith("https://www.npr.org/") for a in articles(app_session))


# ------------------------------------------------- sitemaps: the guards (AC3)


def test_robots_is_read_for_the_sitemaps_own_host(app_session: Session) -> None:
    """NPR's sitemap is on googlecrawl.npr.org. The rule that binds is that host's, and a
    disallow on the publisher's home host says nothing about it."""
    feed = _sitemap_feed(app_session, url="https://sitemaps.example/news.xml")
    fetcher = FakeFetcher(
        {
            "https://sitemaps.example/robots.txt": FetchResult(
                status=200, body=b"User-agent: *\nDisallow: /news.xml\n"
            ),
            f"{SITE}/robots.txt": FetchResult(status=200, body=b"User-agent: *\nAllow: /\n"),
            feed.url: G1_SITEMAP,
        }
    )

    report = _cycle(app_session, fetcher)

    assert fetcher.feeds == []
    assert report.robots_blocked == 1
    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None and state.last_error == "robots: disallowed"


def test_a_home_host_disallow_does_not_block_a_sitemap_elsewhere(app_session: Session) -> None:
    feed = _sitemap_feed(app_session, url="https://sitemaps.example/news.xml")
    fetcher = FakeFetcher(
        {
            f"{SITE}/robots.txt": FetchResult(status=200, body=b"User-agent: *\nDisallow: /\n"),
            feed.url: G1_SITEMAP,
        }
    )

    report = _cycle(app_session, fetcher)

    assert report.discovered == 1
    assert f"{SITE}/robots.txt" not in [url for url, _, _ in fetcher.calls]


@pytest.mark.parametrize(
    ("feed_kw", "source_kw"),
    [
        ({"enabled": False}, {}),
        ({}, {"enabled": False}),
        ({}, {"permitted_to_ingest": False}),
    ],
    ids=["feed disabled", "publisher disabled", "publisher not permitted"],
)
def test_a_gated_sitemap_is_not_polled(
    app_session: Session, feed_kw: dict[str, Any], source_kw: dict[str, Any]
) -> None:
    """AP's news sitemap is registered at ``permitted_to_ingest: false``; this is that case."""
    feed = _feed_with_source(
        app_session,
        url="https://sitemaps.example/news.xml",
        discovery_method=DiscoveryMethod.SITEMAP,
        source={"home_url": SITE, **source_kw},
        **feed_kw,
    )
    fetcher = FakeFetcher({feed.url: G1_SITEMAP})

    report = _cycle(app_session, fetcher)

    assert fetcher.calls == []
    assert report.polled == 0


def test_a_feed_and_a_sitemap_of_one_publisher_share_its_rate_budget(
    app_session: Session,
) -> None:
    feed, sm = _publisher_with_feed_and_sitemap(app_session, rate_limit_per_min=60)
    clock = SimClock(fetch_cost=0.0)

    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}), clock=clock)

    # Four requests to one publisher — a robots.txt per host, then each feed — one gap apart.
    assert clock.slept == [1.0, 1.0, 1.0]


def test_a_sitemap_poll_carries_the_validator_it_was_given(app_session: Session) -> None:
    feed = _sitemap_feed(app_session)
    fetcher = FakeFetcher({feed.url: G1_SITEMAP}, etag='"v1"')

    _cycle(app_session, fetcher)
    report = _cycle(app_session, fetcher)

    assert fetcher.feeds[-1][2].get("If-None-Match") == '"v1"'
    assert report.not_modified == 1


def test_a_refused_sitemap_is_recorded_as_a_failed_poll(app_session: Session) -> None:
    """AP's news sitemap answered 403 with a Cloudflare challenge on 15 and 30 Sep 2026."""
    feed = _sitemap_feed(app_session)

    report = _cycle(
        app_session, FakeFetcher({feed.url: FetchResult(status=403, error="Forbidden")})
    )

    assert (report.polled, report.failed) == (1, 1)
    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None
    assert (state.last_status, state.consecutive_failures) == (403, 1)


def test_a_sitemap_index_is_recorded_as_unreadable_with_the_reason(app_session: Session) -> None:
    feed = _sitemap_feed(app_session)
    raw = (FIXTURES / "bbc_news_sitemap_index.xml").read_bytes()

    report = _cycle(app_session, FakeFetcher({feed.url: raw}))

    assert report.failed == 1
    app_session.expire_all()
    state = poll_state.get(app_session, feed.feed_id)
    assert state is not None and state.last_error is not None
    assert "sitemap index" in state.last_error


def test_an_rss_feed_registered_as_a_sitemap_is_unreadable_not_misread(
    app_session: Session,
) -> None:
    """The method is the registry's statement about the URL, and a mismatch is reported rather
    than guessed past — the same as a feed URL that has rotted into an HTML page."""
    feed = _sitemap_feed(app_session)

    report = _cycle(app_session, FakeFetcher({feed.url: ONE_ITEM}))

    assert (report.failed, report.discovered) == (1, 0)


# ------------------------------------------------- sitemaps: one article, two routes (AC4)


def test_a_publishers_feeds_are_polled_before_its_sitemaps(app_session: Session) -> None:
    feed, sm = _publisher_with_feed_and_sitemap(app_session, sitemap_first=True)
    fetcher = FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP})

    report = _cycle(app_session, fetcher)

    assert [url for url, _, _ in fetcher.feeds] == [feed.url, sm.url]
    assert (report.discovered, report.adopted) == (1, 0)


def _the_record(session: Session) -> tuple[object, ...]:
    (article,) = articles(session)
    return (
        article.feed_id,
        article.guid,
        article.url_canonical,
        article.title,
        article.lede,
        article.body_text,
        article.body_provenance,
        article.published_at,
    )


@pytest.mark.parametrize("first", ["feed", "sitemap"])
def test_the_route_an_article_arrives_by_does_not_change_its_record(
    app_session: Session, first: str
) -> None:
    """AC4's falsifier. The sitemap sees the article a cycle before the feed does — the order a
    within-cycle sort cannot fix — and while acquire has not reached the record, it still ends
    with the feed's teaser and body. Once acquire has, it does not: see the next test."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    if first == "sitemap":
        _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
        assert articles(app_session)[0].lede is None
    report = _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    assert _the_record(app_session) == (
        feed.feed_id,
        "g1",
        f"{SITE}/g1",
        "Floods displace thousands",
        "A teaser.",
        "THE WHOLE ARTICLE",
        BodyProvenance.TIER1_FEED,
        dt.datetime(2026, 8, 25, 9, 14, 2, tzinfo=dt.UTC),
    )
    assert report.adopted == (1 if first == "sitemap" else 0)
    assert report.late_sightings == 0
    assert app_session.scalar(sa.select(sa.func.count()).select_from(PipelineWork)) == 1


def test_a_record_acquire_has_taken_is_counted_as_a_late_sighting(app_session: Session) -> None:
    """The limit of AC4, stated as a test. Acquire runs ten times in a discovery interval, so an
    article a feed lists a cycle after its sitemap has usually been acquired already and keeps
    the sitemap's empty lede. Counted every cycle the feed lists it: the number that says
    whether holding a sitemap's records for the feed is worth a cycle of freshness."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session, rights_level=RightsLevel.HEADLINE_ONLY)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    clock = SimClock(fetch_cost=0.0)
    pacer = Pacer(sleep=clock.sleep, clock=clock)
    fetcher = FakeFetcher({})
    acquired = run_batch(
        app_session,
        network=Network(
            fetcher=fetcher, robots=RobotsCache(fetcher, pacer, clock=clock), pacer=pacer
        ),
        lease=dt.timedelta(minutes=5),
    )

    reports = [
        _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))
        for _ in range(2)
    ]

    assert acquired.acquired == 1
    assert [(r.adopted, r.late_sightings) for r in reports] == [(0, 1), (0, 1)]
    (article,) = articles(app_session)
    assert (article.feed_id, article.lede) == (sm.feed_id, None)


def test_adoption_respects_body_rights(app_session: Session) -> None:
    """The adopted body is the one the feed would have stored: none, for a headline-only
    publisher, whatever the item carries."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session, rights_level=RightsLevel.HEADLINE_ONLY)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))

    _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    (article,) = articles(app_session)
    assert (article.lede, article.body_text, article.body_provenance) == ("A teaser.", None, None)
    assert article.feed_id == feed.feed_id


def test_a_record_already_claimed_by_acquire_is_not_adopted(app_session: Session) -> None:
    """Acquire is reading the record: a raw teaser written under it would be advanced
    uncleaned, and a feed body would land under a page being fetched."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="acquire-1", lease=dt.timedelta(minutes=5)
    )

    report = _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    assert (report.adopted, report.late_sightings) == (0, 0)
    (article,) = articles(app_session)
    assert (article.feed_id, article.lede, article.body_text) == (sm.feed_id, None, None)


def test_a_record_past_acquire_is_not_adopted(app_session: Session) -> None:
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    (work,) = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="acquire-1", lease=dt.timedelta(minutes=5)
    )
    work_queue.advance(app_session, work)
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    assert (report.adopted, report.late_sightings) == (0, 1)
    (article,) = articles(app_session)
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert (article.feed_id, article.lede) == (sm.feed_id, None)


def test_a_dead_lettered_record_is_not_adopted(app_session: Session) -> None:
    """Dead-lettering clears the claim, so an unclaimed row is not enough. The record is the
    evidence a human reads to decide what went wrong; it stays as the failure left it."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    (work,) = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="acquire-1", lease=dt.timedelta(minutes=5)
    )
    work_queue.dead_letter(app_session, work, error="boom")
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    assert report.adopted == 0
    assert articles(app_session)[0].lede is None


def test_one_article_in_two_sitemaps_stays_with_the_first(app_session: Session) -> None:
    """The Conversation's editions list one article in several sitemaps. A sitemap's sighting
    adopts nothing, or the record would move between them on every poll."""
    source = make_source(app_session, home_url=SITE)
    first, second = (
        make_feed(
            app_session,
            source,
            url=f"https://sitemaps.example/{name}.xml",
            discovery_method=DiscoveryMethod.SITEMAP,
            acquisition_tier=AcquisitionTier.EXTRACTION,
        )
        for name in ("first", "second")
    )
    app_session.commit()

    reports = [
        _cycle(app_session, FakeFetcher({first.url: G1_SITEMAP, second.url: G1_SITEMAP}))
        for _ in range(2)
    ]

    assert [r.adopted for r in reports] == [0, 0]
    assert articles(app_session)[0].feed_id == first.feed_id


def _fail_rather_than_wait(session: Session) -> None:
    """Every transaction the session begins gives up on a lock after two seconds, so a cycle
    that waits on a held lock fails the test instead of hanging it.

    ⚠️ ``SET LOCAL`` on each begin, not one ``SET``: the session goes back to the pool at each
    commit and may begin its next transaction on another connection, which never saw a
    session-level setting — and the wait is then unbounded.
    """

    @sa.event.listens_for(session, "after_begin")
    def _lock_timeout(_session: Session, _transaction: object, connection: sa.Connection) -> None:
        connection.exec_driver_sql("SET LOCAL lock_timeout = '2s'")


def test_a_locked_record_is_skipped_not_waited_on(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """A worker or the reconciler holding the article: the cycle moves on rather than queue
    behind it. ``lock_timeout`` turns a wait into a failure here instead of a hang."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    article_id = articles(app_session)[0].article_id
    _fail_rather_than_wait(app_session)

    with Session(app_migrated) as other:
        other.execute(
            sa.select(CanonicalRecord.article_id)
            .where(CanonicalRecord.article_id == article_id)
            .with_for_update()
        )
        report = _cycle(
            app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP})
        )
        other.rollback()

    assert (report.adopted, report.failed) == (0, 0)


def test_a_locked_work_row_is_skipped_not_waited_on(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """Acquire's claim locks the row before it commits; the record is free by then."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    _fail_rather_than_wait(app_session)

    with Session(app_migrated) as other:
        other.execute(sa.select(PipelineWork.work_id).with_for_update())
        report = _cycle(
            app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP})
        )
        other.rollback()

    assert (report.adopted, report.failed) == (0, 0)
    assert articles(app_session)[0].lede is None


def test_another_publishers_record_at_the_url_is_not_adopted(app_session: Session) -> None:
    """A URL two publishers both list is a collision, not a second sighting."""
    _, other_sm = _publisher_with_feed_and_sitemap(app_session)
    mine = _feed_with_source(app_session, url="https://feeds.example/mine.xml")
    _cycle(app_session, FakeFetcher({other_sm.url: G1_SITEMAP}))

    report = _cycle(app_session, FakeFetcher({mine.url: rss_item_with_body()}))

    assert report.adopted == 0
    (article,) = articles(app_session)
    assert (article.feed_id, article.lede) == (other_sm.feed_id, None)


def test_an_rss_sighting_does_not_adopt_another_feeds_record(app_session: Session) -> None:
    """Only a sitemap's record is rewritten: between two feeds the first still wins, as before."""
    source = make_source(app_session, home_url=SITE)
    first = make_feed(app_session, source, url="https://feeds.example/first.xml")
    second = make_feed(app_session, source, url="https://feeds.example/second.xml")
    app_session.commit()
    _cycle(app_session, FakeFetcher({first.url: feed_xml(("g1", "One"), description="")}))

    report = _cycle(
        app_session,
        FakeFetcher(
            {first.url: EMPTY_RSS, second.url: rss_item_with_body(link="https://x.example/g1")}
        ),
    )

    assert report.adopted == 0
    assert articles(app_session)[0].feed_id == first.feed_id


def test_adoption_keeps_the_guid_another_record_already_holds(app_session: Session) -> None:
    """One write must not fail the feed's whole poll: the guid is the one column whose new
    value can collide, under ``UNIQUE(source_id, guid)``."""
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    source = app_session.get(Source, feed.source_id)
    assert source is not None
    make_article(app_session, source, guid="g1", url=f"{SITE}/older")
    app_session.commit()

    report = _cycle(app_session, FakeFetcher({feed.url: rss_item_with_body(), sm.url: G1_SITEMAP}))

    assert (report.adopted, report.failed) == (1, 0)
    adopted = app_session.scalar(
        sa.select(CanonicalRecord).where(CanonicalRecord.url_canonical == f"{SITE}/g1")
    )
    assert adopted is not None
    assert (adopted.guid, adopted.lede) == (f"{SITE}/g1", "A teaser.")


def test_adoption_keeps_the_sitemaps_date_when_the_item_has_none(app_session: Session) -> None:
    feed, sm = _publisher_with_feed_and_sitemap(app_session)
    _cycle(app_session, FakeFetcher({feed.url: EMPTY_RSS, sm.url: G1_SITEMAP}))
    undated = rss_item_with_body().replace(b"<pubDate>Tue, 25 Aug 2026 09:14:02 GMT</pubDate>", b"")

    _cycle(app_session, FakeFetcher({feed.url: undated, sm.url: G1_SITEMAP}))

    assert articles(app_session)[0].published_at == dt.datetime(
        2026, 10, 1, 11, 50, 2, tzinfo=dt.UTC
    )
