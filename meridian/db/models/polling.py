"""What discovery remembers about a feed between polls (FR-I1, FR-I3).

Deliberately not columns on ``Feed``. ``feed.updated_at`` is trigger-maintained and is the
version token every compare-and-set write on the registry compares against, so a poller writing
an ETag onto that row every cycle would move the token every cycle — and every admin page would
go stale within one poll interval, making a feed edit fail with a conflict for no visible
reason. The registry is written by people; this table is written by the poller.
"""

import datetime as dt

import sqlalchemy as sa
from meridian_dbkit import TZDateTime
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class FeedPollState(Base):
    """One row per feed that has been polled at least once.

    Absent until the first poll, so a feed with no row here has never been reached — which is a
    different thing from one whose last poll failed, and worth being able to tell apart.
    """

    __tablename__ = "feed_poll_state"

    #: Also the primary key: one row per feed, and it goes when the feed does.
    feed_id: Mapped[int] = mapped_column(
        sa.ForeignKey("feed.feed_id", ondelete="CASCADE"), primary_key=True
    )

    #: Handed back on the next request as ``If-None-Match``. NULL where the publisher sends no
    #: validator, which is common enough that its absence is not a fault.
    etag: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: The publisher's own ``Last-Modified``, echoed back as ``If-Modified-Since``. Stored as the
    #: raw header string rather than parsed: it is round-tripped, never compared, and reformatting
    #: a date we did not author is how a validator stops matching.
    last_modified: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    last_polled_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    #: HTTP status of the last attempt, or NULL if it never got a response — a timeout, a DNS
    #: failure, a connection reset. ``last_error`` carries which.
    last_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: Consecutive polls that yielded no usable feed — a transport failure or any status other
    #: than 200/304. Reset to zero by a poll that did. Read together with ``last_status`` it
    #: separates the two failures that look identical from a distance: a high count with a 404
    #: is a rotted URL and someone must find the new one, a high count with no status at all is
    #: a publisher we cannot reach. Both were live on the v1 roster.
    consecutive_failures: Mapped[int] = mapped_column(sa.Integer, server_default="0")
