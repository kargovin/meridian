"""What discovery remembers about a feed between polls.

Split from ``meridian.db.feeds`` for the reason the table is split from ``feed``: this is the
poller's own state, written on every cycle, and it must not touch a registry row whose
``updated_at`` is a version token someone else is comparing against.

None of these commit. The caller owns the transaction.
"""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from meridian.db.models import FeedPollState

#: Statuses that mean the publisher answered us properly: the feed body, or "unchanged since
#: the validator you sent me".
_REACHED = (200, 304)


def get(session: Session, feed_id: int) -> FeedPollState | None:
    return session.get(FeedPollState, feed_id)


def for_feeds(session: Session, feed_ids: Sequence[int]) -> dict[int, FeedPollState]:
    """Poll state for a set of feeds, keyed by feed id. Missing = never polled."""
    if not feed_ids:
        return {}
    found = session.scalars(
        sa.select(FeedPollState).where(FeedPollState.feed_id.in_(feed_ids))
    ).all()
    return {state.feed_id: state for state in found}


def validators(state: FeedPollState | None) -> dict[str, str]:
    """Conditional-request headers from what the publisher last told us.

    Empty for a feed never polled, or one whose publisher sends no validator — in which case
    every poll transfers the whole feed, which is the publisher's choice and not a fault.
    """
    if state is None:
        return {}
    headers = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified
    return headers


def record(
    session: Session,
    feed_id: int,
    *,
    status: int | None,
    etag: str | None = None,
    last_modified: str | None = None,
    error: str | None = None,
    now: dt.datetime | None = None,
) -> None:
    """Write the outcome of one poll, creating the row on first contact.

    ``status`` is None when there was no response at all — a timeout, a refused connection, a
    DNS failure — and ``error`` says which.

    ⚠️ Validators are only overwritten when the publisher sends them. A 304 carries no body and
    frequently no ``ETag``, so writing the absent value through would clear the validator that
    just produced the 304 and make the next poll unconditional — the saving would disappear
    after exactly one successful use of it.
    """
    now = now or dt.datetime.now(dt.UTC)
    reached = status in _REACHED

    values: dict[str, object] = {
        "feed_id": feed_id,
        "last_polled_at": now,
        "last_status": status,
        "last_error": error,
        "consecutive_failures": 0 if reached else 1,
    }
    updates: dict[str, object] = {
        "last_polled_at": now,
        "last_status": status,
        "last_error": error,
        "consecutive_failures": (0 if reached else FeedPollState.consecutive_failures + 1),
    }
    if etag is not None:
        values["etag"] = etag
        updates["etag"] = etag
    if last_modified is not None:
        values["last_modified"] = last_modified
        updates["last_modified"] = last_modified

    session.execute(
        insert(FeedPollState)
        .values(**values)
        .on_conflict_do_update(index_elements=[FeedPollState.feed_id], set_=updates)
    )
