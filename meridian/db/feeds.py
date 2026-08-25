"""Reading and writing the feed registry (RFC §5.1, rev 23).

A ``Feed`` is one URL discovery polls; a ``Source`` is the publisher it belongs to. Everything
that reads or writes a feed goes through here rather than issuing its own query, for the same
reason ``sources`` exists: a rule that gains a second condition gains it everywhere at once.

The write discipline is the one ``sources`` establishes — governing fields get their own
single-field route and are compare-and-set against ``updated_at`` under ``FOR UPDATE``, while
descriptive fields are written as a form submits them. ``acquisition_tier`` keeps the governing
treatment it had before it moved here: a field does not become safer by changing tables.

None of these commit. The caller owns the transaction.
"""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod
from sqlalchemy.orm import Session

from meridian.db.models import Feed, Source
from meridian.db.sources import StaleWrite


def for_source(session: Session, source_id: int) -> Sequence[Feed]:
    """Every feed of one publisher, enabled or not — the admin list."""
    return session.scalars(
        sa.select(Feed).where(Feed.source_id == source_id).order_by(Feed.name)
    ).all()


def get(session: Session, feed_id: int) -> Feed | None:
    return session.get(Feed, feed_id)


def counts_by_source(session: Session) -> dict[int, int]:
    """How many feeds each publisher has, for the admin list.

    Returned as a mapping rather than as a relationship on ``Source`` deliberately. A lazy
    relationship read from a template loads after the request's session has been closed, and an
    eager one makes every caller of ``sources.list_all`` pay for a join it does not want. A
    publisher missing from this mapping has no feeds, which is worth showing: it polls nothing,
    and no other column on its row says so.
    """
    rows = session.execute(
        sa.select(Feed.source_id, sa.func.count()).group_by(Feed.source_id)
    ).all()
    return {source_id: count for source_id, count in rows}


def pollable(session: Session) -> Sequence[tuple[Feed, Source]]:
    """The feeds discovery may poll, each with its publisher — all three gates, never a subset.

    A feed is polled only if the feed itself is enabled *and* its publisher is both
    operationally enabled and permitted to ingest. The three live on two tables and are set by
    different people for different reasons, which is precisely the situation in which a caller
    checks the nearest one and misses the others: disabling a publisher would otherwise leave
    its feeds polling, because nothing about the feed row changed.

    The publisher comes back with the feed because every caller needs it — for the rate limit
    and the user agent — and fetching it separately makes two statements out of one. Under READ
    COMMITTED those two can disagree: a publisher deleted between them is absent from the second
    result, and a caller that indexes into it raises where it expected a row.

    Read per run, never cached — FR-I6 exists so a Legal or ToS problem stops ingestion in
    minutes, and a cache adds its own lifetime to that number.
    """
    return session.execute(
        sa.select(Feed, Source)
        .join(Source, Source.source_id == Feed.source_id)
        .where(
            Feed.enabled.is_(True),
            Source.enabled.is_(True),
            Source.permitted_to_ingest.is_(True),
        )
        .order_by(Feed.feed_id)
    ).all()  # type: ignore[return-value]


def create(
    session: Session,
    *,
    source_id: int,
    name: str,
    url: str,
    discovery_method: DiscoveryMethod,
    acquisition_tier: AcquisitionTier,
    enabled: bool = True,
) -> Feed:
    feed = Feed(
        source_id=source_id,
        name=name,
        url=url,
        discovery_method=discovery_method,
        acquisition_tier=acquisition_tier,
        enabled=enabled,
    )
    session.add(feed)
    session.flush()
    return feed


def describe(
    session: Session,
    feed_id: int,
    *,
    name: str,
    url: str,
    discovery_method: DiscoveryMethod,
) -> Feed | None:
    """Write the descriptive fields, as the edit form submits them.

    ``enabled`` and ``acquisition_tier`` are deliberately absent, each having its own
    single-field setter — see the ``sources.describe`` docstring for why a descriptive form must
    not carry a governing field. ``discovery_method`` is here rather than on its own route
    because a stale value costs a failed poll, not a permission: it says how to read a URL, and
    no value of it lets us fetch something we may not fetch.
    """
    feed = session.get(Feed, feed_id)
    if feed is None:
        return None
    feed.name = name
    feed.url = url
    feed.discovery_method = discovery_method
    session.flush()
    return feed


def delete(session: Session, feed_id: int) -> bool:
    """Remove a feed. Records it discovered keep their ``feed_id`` set to NULL, not deleted.

    Deleting is legitimate here in a way it is not for a publisher: a feed is a URL we poll, and
    a retired one is not history worth keeping. Disabling is the softer option and is what an
    operator should reach for first, which is why both exist.
    """
    feed = session.get(Feed, feed_id)
    if feed is None:
        return False
    session.delete(feed)
    session.flush()
    return True


def _claim(session: Session, feed_id: int, expected_updated_at: dt.datetime) -> Feed | None:
    feed = session.get(Feed, feed_id)
    if feed is None:
        return None
    # SELECT ... FOR UPDATE, so the compare and the write are one step. Without the lock two
    # requests both pass the check and the second blocks on the row only at flush, applying on
    # top — the lost update this mechanism exists to prevent. See sources._claim.
    session.refresh(feed, with_for_update=True)
    if feed.updated_at != expected_updated_at:
        raise StaleWrite(
            f"feed {feed_id} changed at {feed.updated_at.isoformat()}, "
            f"after the page offering this write was rendered"
        )
    return feed


def set_enabled(
    session: Session, feed_id: int, *, value: bool, expected_updated_at: dt.datetime
) -> Feed | None:
    """Stop or resume polling one feed.

    Maintenance rather than a Legal stop — a feed URL that has been retired or moved is an
    ordinary event, and both are live on the v1 roster. The Legal stop is
    ``sources.set_permitted_to_ingest``; sharing one column between them would make a routine
    change indistinguishable from an emergency one in the place someone looks during an
    incident.
    """
    feed = _claim(session, feed_id, expected_updated_at)
    if feed is None:
        return None
    feed.enabled = value
    session.flush()
    return feed


def set_acquisition_tier(
    session: Session,
    feed_id: int,
    *,
    tier: AcquisitionTier,
    expected_updated_at: dt.datetime,
) -> Feed | None:
    """Change how this feed's bodies are obtained, for the same reason as ``set_enabled``.

    This is the one field on either registry table with more than two values, so it is the one
    where a lost update is not saved by the harmful write being a no-op the ORM elides.
    """
    feed = _claim(session, feed_id, expected_updated_at)
    if feed is None:
        return None
    feed.acquisition_tier = tier
    session.flush()
    return feed
