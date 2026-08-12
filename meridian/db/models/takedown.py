"""Takedown tombstones (RFC §5.1/§10, Legal assumption A-L2)."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import TakedownScope
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base
from meridian.db.types import Sha256, StrEnumType, TZDateTime, enum_check


class Takedown(Base):
    """A record of what must not be re-ingested after a publisher removal request.

    Keyed on the *external* identity, not ``article_id``: the row that id named has been
    deleted, and an incoming feed item does not know our internal ids. Discovery checks this
    table **before insert** — checking after would recreate the record on every poll and
    delete it on a timer.

    Holds no content. A tombstone that retains the article defeats the takedown.
    """

    __tablename__ = "takedown"
    __table_args__ = (
        sa.Index("ix_takedown_source_id_guid", "source_id", "guid"),
        sa.Index("ix_takedown_url_canonical", "url_canonical"),
        sa.Index("ix_takedown_content_hash", "content_hash"),
        enum_check("scope", TakedownScope),
    )

    takedown_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)

    source_id: Mapped[int] = mapped_column(sa.ForeignKey("source.source_id", ondelete="RESTRICT"))
    guid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    url_canonical: Mapped[str] = mapped_column(sa.Text)
    content_hash: Mapped[str | None] = mapped_column(Sha256, nullable=True)

    #: Audit only, deliberately no FK — the row it names is gone.
    removed_article_id: Mapped[int] = mapped_column(sa.BigInteger)

    #: Which case this was, so the distinct-source count knows what it lost.
    scope: Mapped[TakedownScope] = mapped_column(StrEnumType(TakedownScope))

    reason: Mapped[str] = mapped_column(sa.Text)
    requested_at: Mapped[dt.datetime] = mapped_column(TZDateTime)
    actioned_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
