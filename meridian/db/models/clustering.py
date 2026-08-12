"""Clusters, membership and summaries (RFC §5.1, FR-K1..K4 / FR-S1..S6)."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import WindowStatus, WithholdReason
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base
from meridian.db.types import Sha256, StrEnumType, TZDateTime, enum_check


class Cluster(Base):
    """A group of articles covering one event.

    Embedding vectors are deliberately absent — the working set is an in-memory numpy array
    rebuilt from title+lede on restart (A6).
    """

    __tablename__ = "cluster"
    __table_args__ = (
        sa.Index("ix_cluster_window_status", "window_status"),
        enum_check("window_status", WindowStatus),
    )

    cluster_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)

    #: Confidence-weighted vote over members. Unconstrained text, as Classification.topic.
    representative_topic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Straddle signal: how close the vote was. Logged for threshold tuning.
    topic_margin: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    article_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    #: Gates summarization at >= 2 (FR-S6). Counted as DISTINCT source_id over the members
    #: and their alternate copies — never a row count, or one publisher's reissue reads as a
    #: second source.
    distinct_source_count: Mapped[int] = mapped_column(sa.Integer, server_default="0")

    earliest_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
    latest_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)

    window_status: Mapped[WindowStatus] = mapped_column(
        StrEnumType(WindowStatus), server_default=WindowStatus.OPEN.value
    )


class ClusterMember(Base):
    """Membership, 1:n — an article belongs to at most one cluster.

    Enforced by the primary key: ``article_id`` alone is the PK, so a second row for the same
    article raises. Reconciliation therefore *moves* membership with an UPDATE and has no way
    to duplicate it. Current membership only — the previous cluster is gone after the UPDATE.
    """

    __tablename__ = "cluster_member"
    __table_args__ = (sa.Index("ix_cluster_member_cluster_id", "cluster_id"),)

    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("canonical_record.article_id", ondelete="CASCADE"), primary_key=True
    )
    cluster_id: Mapped[int] = mapped_column(sa.ForeignKey("cluster.cluster_id", ondelete="CASCADE"))


class Summary(Base):
    """The current summary for a cluster — one row, always present once attempted.

    A withheld summary is a row with ``withheld`` set and a reason, never an absent row: an
    absent row cannot distinguish "withheld" from "not summarized yet", which is exactly the
    distinction the read model has to preserve.
    """

    __tablename__ = "summary"
    __table_args__ = (
        # The flag and the reason must agree, or the read model renders "no summary" with
        # nothing to say about why.
        sa.CheckConstraint(
            "(withheld IS FALSE AND withhold_reason = 'none')"
            " OR (withheld IS TRUE AND withhold_reason <> 'none')",
            name="withheld_matches_reason",
        ),
        enum_check("withhold_reason", WithholdReason),
    )

    cluster_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cluster.cluster_id", ondelete="CASCADE"), primary_key=True
    )

    #: NULL whenever withheld.
    text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    faithfulness_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    withheld: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.false())
    withhold_reason: Mapped[WithholdReason] = mapped_column(
        StrEnumType(WithholdReason),
        server_default=WithholdReason.NONE.value,
    )

    #: FR-S3 attribution.
    provenance_urls: Mapped[list[str]] = mapped_column(sa.ARRAY(sa.Text), server_default="{}")

    model_version: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    generated_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)

    #: sha256 over member ids and their content hashes — answers "which text did we
    #: summarize" now that the row is overwritten in place.
    input_fingerprint: Mapped[str | None] = mapped_column(Sha256, nullable=True)
