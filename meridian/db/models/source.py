"""The source registry (RFC §5.1). Governs what the pipeline may do with each publisher."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from meridian_dbkit import StrEnumType, TZDateTime, enum_check
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class Source(Base):
    """One publisher. What discovery actually polls is a ``Feed`` beneath it.

    ``source_id`` is the identity FR-S5 rights and the FR-S6 distinct-source count both key on,
    which is why it names a publisher and not a feed: two feeds for one publisher would
    otherwise read as two sources and promote a single-publisher cluster past the ≥2-source
    gate, with nothing malfunctioning.
    """

    __tablename__ = "source"
    __table_args__ = (
        enum_check("rights_level", RightsLevel),
        # FR-I3 politeness has no meaning at zero, and the pipeline's obvious use of it
        # (60 / rate) has no defined behaviour there.
        sa.CheckConstraint("rate_limit_per_min > 0", name="rate_limit_per_min_positive"),
    )

    source_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text)
    #: Descriptive, not an identity: nothing canonicalises it, so a trailing slash or a
    #: ``www.`` makes a second row for the same publisher. See RFC §11.
    home_url: Mapped[str] = mapped_column(sa.Text)

    #: May we ingest this publisher at all. Read before ``rights_level``, not as part of it: a
    #: terms-of-service exclusion is not a lower rung on the body_text/headline_only ladder, so
    #: recording one as ``headline_only`` leaves the pipeline politely still polling a publisher
    #: whose terms forbid the whole activity. robots.txt and the terms are different artifacts
    #: and only the first is machine-readable, so this is a human determination.
    permitted_to_ingest: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())

    rights_level: Mapped[RightsLevel] = mapped_column(StrEnumType(RightsLevel))

    jurisdiction: Mapped[str] = mapped_column(sa.Text)

    #: FR-I6: stop ingestion without a deploy. Operational — the Legal stop is
    #: ``permitted_to_ingest``, and a dead feed URL is ``Feed.enabled``.
    enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())

    #: FR-I3 politeness, per publisher rather than per feed. The number is a promise about one
    #: host: N feeds each honouring it independently would exceed it N-fold, so the poller
    #: shares one budget across a publisher's feeds.
    rate_limit_per_min: Mapped[int] = mapped_column(sa.Integer)

    #: NULL means the caller's default. Some edges black-hole a User-Agent that carries a
    #: contact URL — measured, the request hangs to the read timeout rather than being
    #: refused, so it costs the whole timeout budget and reads as an outage. No single value is
    #: right everywhere and the right one is found by being burned, so it is registry data.
    user_agent: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    #: Operational staleness only — has anyone touched this row, and when. NOT a history: the
    #: next unrelated edit overwrites it, so it cannot say when a particular field changed.
    #: Maintained by a database trigger rather than the ORM, because the emergency path is a
    #: psql session and a column that lies on exactly that path is worse than no column.
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())


class Feed(Base):
    """One URL discovery polls, belonging to a ``Source`` (FR-I1).

    Separate from the publisher because the two are governed by different people and change for
    different reasons: rights and politeness are determinations about an outlet, while a URL, a
    discovery method and whether bodies arrive in-feed are facts about one feed and vary within
    a publisher.
    """

    __tablename__ = "feed"
    __table_args__ = (
        # Two rows polling one URL is duplicate work against a publisher whose rate limit is
        # shared, and it re-collapses the same items on every poll.
        sa.UniqueConstraint("url"),
        sa.Index("ix_feed_source_id", "source_id"),
        enum_check("discovery_method", DiscoveryMethod),
        enum_check("acquisition_tier", AcquisitionTier),
    )

    feed_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(sa.ForeignKey("source.source_id", ondelete="CASCADE"))

    #: Distinguishes a publisher's feeds from each other in the admin list — "World", "UK".
    name: Mapped[str] = mapped_column(sa.Text)
    url: Mapped[str] = mapped_column(sa.Text)

    #: Per feed, not per publisher: one outlet can disallow its RSS paths in robots.txt while
    #: allowing articles, which makes it a sitemap source without changing what it is.
    discovery_method: Mapped[DiscoveryMethod] = mapped_column(StrEnumType(DiscoveryMethod))
    #: Whether a body arrives in-feed is a fact about the feed.
    acquisition_tier: Mapped[AcquisitionTier] = mapped_column(StrEnumType(AcquisitionTier))

    #: Stop polling this URL. Maintenance, not a Legal stop — a retired or moved feed is an
    #: ordinary event, and sharing a column with ``Source.enabled`` would make one look like the
    #: other in the place someone looks during an incident.
    enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())

    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    #: Trigger-maintained, like ``Source.updated_at``, and used as the same version token.
    updated_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
