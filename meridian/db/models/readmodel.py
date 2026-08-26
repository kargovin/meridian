"""The read model — what the reader surface reads (RFC §6.2/§6.3, FR-V1).

A projection, not a source of truth: every column here is copied or derived from the
entities in this package, and truncating both tables loses nothing that
``meridian.readmodel.project`` cannot recompute. That property is what allows the
denormalization; without it the cache has quietly become the record.

It exists so the read path cannot reach a model (A4). Summarization takes seconds per
cluster, so a page that could trigger one would hang; here the answer is already a row by
the time anyone asks. Nothing on the read path joins, aggregates, or calls the Platform.
"""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import WithholdReason
from meridian_dbkit import StrEnumType, TZDateTime, enum_check
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class ClusterProjection(Base):
    """One row per readable cluster — the topic-browse row and the head of the detail page.

    A cluster is projected once it has members, not once it is summarized. Under FR-S6 a
    cluster is summarized only at >= 2 distinct sources, so most clusters never are; and a
    withheld cluster (FR-S2/FR-S5) is fully processed and must still be readable. Waiting
    for a summary would leave the surface almost empty and would be indistinguishable from
    a broken pipeline.
    """

    __tablename__ = "cluster_projection"
    __table_args__ = (
        # The FR-V1 browse query: clusters of one topic, most recent first.
        sa.Index("ix_cluster_projection_topic_latest_at", "topic", sa.text("latest_at DESC")),
        # Mirrors summary.withheld_matches_reason. The read model is the last place a body
        # we hold no rights to could reach a reader, so a withheld row may not carry text.
        # NULL reason means no summary row exists yet, which must also carry no text.
        sa.CheckConstraint(
            "(withhold_reason IS NULL AND summary_text IS NULL)"
            " OR (withhold_reason = 'none')"
            " OR (withhold_reason <> 'none' AND summary_text IS NULL)",
            name="summary_matches_withhold_reason",
        ),
        enum_check("withhold_reason", WithholdReason),
    )

    cluster_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cluster.cluster_id", ondelete="CASCADE"), primary_key=True
    )

    #: NULL until the cluster-topic vote has run. Such a cluster appears under no topic,
    #: which is correct: there is no bucket to put it in.
    topic: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: The earliest member's title and lede. The lede is projected rather than joined
    #: because it is what a cluster with no summary renders instead, so leaving it out would
    #: send exactly the FR-S6 case that needs it back to ``canonical_record``.
    headline: Mapped[str] = mapped_column(sa.Text)
    lede: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    summary_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: NULL and ``none`` are different facts: no summary row yet, versus summarized and not
    #: withheld. An absent reason cannot distinguish "withheld because X" from "not yet",
    #: which is the distinction the read model exists to preserve (RFC §5.2).
    withhold_reason: Mapped[WithholdReason | None] = mapped_column(
        StrEnumType(WithholdReason), nullable=True
    )

    article_count: Mapped[int] = mapped_column(sa.Integer)
    #: Copied from ``Cluster``, never counted from ``cluster_projection_source``. FR-S6
    #: gates summarization on the write model's number, so a read model computing its own
    #: could show two sources for a cluster the pipeline treats as single-source.
    distinct_source_count: Mapped[int] = mapped_column(sa.Integer)

    earliest_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
    latest_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)


class ClusterProjectionSource(Base):
    """Who covered this story — one row per distinct publisher in the cluster.

    Per publisher, not per article: this is the list beside "also reported by", and it is
    the same identity FR-S6 counts. A publisher running two pieces on one story is one
    entry. Collapsed duplicates contribute too — an ``AlternateCopy`` is a publisher whose
    article was deduplicated away, and dropping it here would lose exactly the provenance
    collapse-not-drop keeps (FR-I5, US-K2).
    """

    __tablename__ = "cluster_projection_source"

    cluster_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cluster_projection.cluster_id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[int] = mapped_column(
        sa.ForeignKey("source.source_id", ondelete="CASCADE"), primary_key=True
    )

    #: Denormalized, which is the point — rendering the list must not join back to
    #: ``source``. A publisher rename reaches the surface on the next projection.
    source_name: Mapped[str] = mapped_column(sa.Text)
    #: This publisher's earliest item in the cluster.
    url: Mapped[str] = mapped_column(sa.Text)
