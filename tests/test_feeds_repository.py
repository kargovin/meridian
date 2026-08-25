"""The feed registry's read and write paths (FR-I1, FR-I6).

A feed is what discovery polls; the publisher above it is what rights and the FR-S6 count key
on. These cover the seam between the two — most of what can go wrong here is a gate checked on
one table when it lives on the other.
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from meridian.db import feeds, sources
from meridian.db.models import CanonicalRecord, Source
from meridian.db.session import session_factory
from tests.factories import make_article, make_feed, make_source

NOW = dt.datetime.now(dt.UTC)


def test_create_then_read_back(app_session: Session) -> None:
    source = make_source(app_session)

    created = feeds.create(
        app_session,
        source_id=source.source_id,
        name="World",
        url="https://feeds.example/world.xml",
        discovery_method=DiscoveryMethod.RSS,
        acquisition_tier=AcquisitionTier.EXTRACTION,
    )

    fetched = feeds.get(app_session, created.feed_id)
    assert fetched is not None
    assert fetched.url == "https://feeds.example/world.xml"
    assert fetched.enabled is True


def test_two_feeds_cannot_share_a_url(app_session: Session) -> None:
    """Two rows polling one URL is duplicate work against a shared rate limit."""
    source = make_source(app_session)
    feeds.create(
        app_session,
        source_id=source.source_id,
        name="A",
        url="https://feeds.example/same.xml",
        discovery_method=DiscoveryMethod.RSS,
        acquisition_tier=AcquisitionTier.EXTRACTION,
    )

    with pytest.raises(IntegrityError):
        feeds.create(
            app_session,
            source_id=source.source_id,
            name="B",
            url="https://feeds.example/same.xml",
            discovery_method=DiscoveryMethod.RSS,
            acquisition_tier=AcquisitionTier.EXTRACTION,
        )


# ------------------------------------------------------------------ the three gates


@pytest.mark.parametrize("gate", ["feed.enabled", "source.enabled", "source.permitted"])
def test_a_feed_is_pollable_only_when_all_three_gates_are_open(
    app_session: Session, gate: str
) -> None:
    """The gates live on two tables, which is exactly how one gets missed.

    Disabling a publisher changes nothing about its feed rows, so a poller reading only the
    feed would keep polling a stopped publisher with nothing looking wrong. Each gate is closed
    on its own, against a fresh pair of rows, so a test passing because a previous case already
    closed a different gate is not possible.
    """
    source = make_source(app_session, name="Outlet")
    feed = make_feed(app_session, source)
    assert [(f.feed_id, s.source_id) for f, s in feeds.pollable(app_session)] == [
        (feed.feed_id, source.source_id)
    ]

    if gate == "feed.enabled":
        feeds.set_enabled(
            app_session, feed.feed_id, value=False, expected_updated_at=feed.updated_at
        )
    elif gate == "source.enabled":
        sources.set_enabled(
            app_session, source.source_id, value=False, expected_updated_at=source.updated_at
        )
    else:
        sources.set_permitted_to_ingest(
            app_session, source.source_id, value=False, expected_updated_at=source.updated_at
        )
    app_session.flush()

    assert feeds.pollable(app_session) == [], f"{gate} did not gate polling"


def test_feeds_of_one_publisher_are_listed_together(app_session: Session) -> None:
    one, two = make_source(app_session, name="One"), make_source(app_session, name="Two")
    a = make_feed(app_session, one, name="A")
    b = make_feed(app_session, one, name="B")
    make_feed(app_session, two, name="C")

    assert [f.feed_id for f in feeds.for_source(app_session, one.source_id)] == [
        a.feed_id,
        b.feed_id,
    ]
    assert feeds.counts_by_source(app_session) == {one.source_id: 2, two.source_id: 1}


def test_a_publisher_with_no_feeds_is_absent_from_the_counts(app_session: Session) -> None:
    """It polls nothing, and the admin list shows that only if the mapping omits it."""
    source = make_source(app_session)

    assert source.source_id not in feeds.counts_by_source(app_session)


# ------------------------------------------------------------------ writes


def test_describe_cannot_touch_the_governing_fields(app_session: Session) -> None:
    source = make_source(app_session)
    feed = make_feed(
        app_session, source, enabled=False, acquisition_tier=AcquisitionTier.EXTRACTION
    )

    feeds.describe(
        app_session,
        feed.feed_id,
        name="Renamed",
        url="https://feeds.example/renamed.xml",
        discovery_method=DiscoveryMethod.SITEMAP,
    )

    fetched = feeds.get(app_session, feed.feed_id)
    assert fetched is not None
    assert fetched.name == "Renamed"
    assert fetched.discovery_method is DiscoveryMethod.SITEMAP
    assert fetched.enabled is False
    assert fetched.acquisition_tier is AcquisitionTier.EXTRACTION


def test_a_governing_write_from_a_stale_page_is_refused(app_session: Session) -> None:
    """Compare-and-set on a feed, for the same reason as on a publisher.

    ``commit`` between the two writes rather than ``flush``, and that is not incidental:
    ``updated_at`` is set from ``now()``, which is transaction start, so two writes inside one
    transaction carry an identical token and the second would be accepted. The version token
    distinguishes transactions, not statements.
    """
    source = make_source(app_session)
    feed = make_feed(app_session, source)
    app_session.commit()
    as_rendered = feed.updated_at

    feeds.set_enabled(app_session, feed.feed_id, value=False, expected_updated_at=as_rendered)
    app_session.commit()

    with pytest.raises(sources.StaleWrite):
        feeds.set_acquisition_tier(
            app_session,
            feed.feed_id,
            tier=AcquisitionTier.EXTRACTION,
            expected_updated_at=as_rendered,
        )

    app_session.rollback()
    after = feeds.get(app_session, feed.feed_id)
    assert after is not None
    assert after.enabled is False


def test_the_targeted_setters_report_an_unknown_id(app_session: Session) -> None:
    assert feeds.set_enabled(app_session, 999_999, value=False, expected_updated_at=NOW) is None
    assert (
        feeds.set_acquisition_tier(
            app_session, 999_999, tier=AcquisitionTier.EXTRACTION, expected_updated_at=NOW
        )
        is None
    )
    assert feeds.delete(app_session, 999_999) is False


def test_deleting_a_feed_keeps_the_articles_it_found(app_session: Session) -> None:
    """``ondelete="SET NULL"``, not CASCADE.

    A feed is a URL we poll and a retired one is disposable; the articles it discovered are
    not, and deleting a rotted feed URL must not delete a month of records with it.
    """
    source = make_source(app_session)
    feed = make_feed(app_session, source)
    article = make_article(app_session, source, guid="kept", feed_id=feed.feed_id)

    assert feeds.delete(app_session, feed.feed_id) is True
    app_session.flush()
    app_session.expire_all()

    kept = app_session.get(CanonicalRecord, article.article_id)
    assert kept is not None
    assert kept.feed_id is None


def test_deleting_a_publisher_deletes_its_feeds(app_session: Session) -> None:
    """CASCADE here, because a feed without its publisher is not a thing that can be polled."""
    source = make_source(app_session)
    feed = make_feed(app_session, source)

    feed_id = feed.feed_id
    app_session.delete(source)
    app_session.flush()

    # Asked of the database, not through the session. The cascade is PostgreSQL's — there is no
    # ORM relationship configured — so the session still holds the Feed it loaded, and
    # ``Session.get`` on it raises rather than answering.
    remaining = app_session.scalar(
        sa.select(sa.func.count()).select_from(sa.text("feed")).where(sa.text("feed_id = :i")),
        {"i": feed_id},
    )
    assert remaining == 0


# ------------------------------------------------------------------ the identity that matters


def test_two_feeds_of_one_publisher_are_one_distinct_source(app_session: Session) -> None:
    """The defect this whole entity change exists to fix (FR-S6, RFC §5.2).

    A publisher's section feeds legitimately carry the same story. While a registry row *was* a
    feed, two such rows were two ``source_id``s, and one story arriving through both counted as
    two distinct sources — enough to promote a single-publisher cluster past the ≥2-source gate
    with nothing malfunctioning. Keyed on the publisher it counts one, which is the truth.

    Asserted on the identity rather than on the count expression, because the counting rule
    itself is not built yet: what has to be true first is that the two arrivals share a
    ``source_id``.
    """
    source = make_source(app_session, name="Broadsheet")
    world = make_feed(app_session, source, name="World")
    politics = make_feed(app_session, source, name="Politics")

    first = make_article(app_session, source, guid="story-1", feed_id=world.feed_id)
    # The same story reaching us through the publisher's other feed, under a different guid.
    second = make_article(app_session, source, guid="story-1-uk", feed_id=politics.feed_id)

    distinct = app_session.scalars(
        sa.select(sa.func.count(sa.distinct(CanonicalRecord.source_id))).where(
            CanonicalRecord.article_id.in_([first.article_id, second.article_id])
        )
    ).one()
    assert distinct == 1, "two feeds of one publisher must not read as two sources"
    assert first.feed_id != second.feed_id, "which feed found it is still answerable"


def test_one_publisher_cannot_publish_the_same_guid_twice(app_session: Session) -> None:
    """UNIQUE(source_id, guid) is publisher-keyed now, so the collapse happens at insert.

    Two section feeds carrying the identical feed item never become two records at all, which
    is a stronger guarantee than dedup recognising them afterwards.
    """
    source = make_source(app_session, name="Broadsheet")
    world = make_feed(app_session, source, name="World")
    politics = make_feed(app_session, source, name="Politics")
    make_article(app_session, source, guid="same-guid", feed_id=world.feed_id)

    with pytest.raises(IntegrityError):
        make_article(
            app_session,
            source,
            guid="same-guid",
            url="https://example.test/other",
            feed_id=politics.feed_id,
        )


# ------------------------------------------------------------------ concurrency


def test_the_version_check_holds_the_row_until_the_write_lands(
    app_migrated: sa.Engine,
) -> None:
    """The compare and the write must be one step.

    Without a lock on the read both requests pass the version check, and the second blocks on
    the row only at flush time — then applies on top, which is the lost update this mechanism
    exists to prevent. Three-valued ``acquisition_tier`` is where that is constructible; the
    two-valued fields escape by accident, because their harmful direction writes back the value
    just read and SQLAlchemy elides it. That accident ends the day one of them gains a member,
    which is why the lock is on ``_claim`` rather than on this field.

    Asserted as the property rather than the SQL: once one session has claimed the row, a
    second cannot reach it. Two sessions in one thread, with ``lock_timeout`` standing in for
    the second request, so the interleaving is deterministic rather than scheduled.
    """
    factory = session_factory(app_migrated)
    with factory() as setup:
        setup.execute(sa.text("TRUNCATE canonical_record, feed, source RESTART IDENTITY CASCADE"))
        source = make_source(setup, rights_level=RightsLevel.BODY_TEXT)
        feed = make_feed(setup, source, acquisition_tier=AcquisitionTier.FULL_FEED)
        setup.commit()
        feed_id, token = feed.feed_id, feed.updated_at

    with factory() as holder, factory() as other:
        claimed = feeds._claim(holder, feed_id, token)
        assert claimed is not None

        other.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
        with pytest.raises(OperationalError):
            feeds.set_acquisition_tier(
                other,
                feed_id,
                tier=AcquisitionTier.PUBLISHER_API,
                expected_updated_at=token,
            )
        other.rollback()

        claimed.acquisition_tier = AcquisitionTier.EXTRACTION
        holder.commit()

    with factory() as check:
        after = feeds.get(check, feed_id)
        assert after is not None
        assert after.acquisition_tier is AcquisitionTier.EXTRACTION


def test_pollable_returns_each_feeds_publisher_with_it(app_session: Session) -> None:
    """⚠️ Together, in one statement, deliberately.

    Read separately, the feed list and the publisher list are two statements under READ
    COMMITTED and can disagree about what exists — a publisher deleted between them is missing
    from the second, and the caller's lookup raises. In the poller that lookup sat outside the
    per-feed guard, so one deleted publisher took the rest of the roster's polling with it.
    """
    first = make_source(app_session, name="First")
    second = make_source(app_session, name="Second")
    a = make_feed(app_session, first)
    b = make_feed(app_session, second, url="https://feeds.example/second.xml")

    pairs = feeds.pollable(app_session)

    # ⚠️ Assert on what only a publisher carries. Feed has a source_id of its own, so a pair of
    # (Feed, Feed) satisfies any assertion written in terms of that column — the first version of
    # this test passed with the join returning the wrong entity.
    by_feed = {feed.feed_id: publisher for feed, publisher in pairs}
    assert set(by_feed) == {a.feed_id, b.feed_id}
    for publisher in by_feed.values():
        assert isinstance(publisher, Source)
    assert by_feed[a.feed_id].name == "First"
    assert by_feed[b.feed_id].name == "Second"
    # The two fields the poller reads off the publisher: FR-I3 pacing and the user agent.
    assert by_feed[a.feed_id].rate_limit_per_min == first.rate_limit_per_min
