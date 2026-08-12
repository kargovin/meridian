"""The work queue (RFC §5.1/§6.2, T5)."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import Stage
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base
from meridian.db.types import StrEnumType, TZDateTime, enum_check


class PipelineWork(Base):
    """What is owed right now. Derived and disposable — truncating it loses nothing.

    Rows are deleted on success and retained on terminal failure, so a systematic failure
    leaves a trace rather than an article that merely stopped moving.

    Claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``, claim-and-commit: mark the row, commit,
    then do the work. Holding the transaction open for the duration would mean a two-minute
    summarize holding a write lock for two minutes.
    """

    __tablename__ = "pipeline_work"
    __table_args__ = (
        sa.CheckConstraint(
            "(article_id IS NULL) <> (cluster_id IS NULL)",
            name="exactly_one_subject",
        ),
        enum_check("stage", Stage),
        # At most one open row per subject. This is also the debounce: a second change
        # arriving while an item is pending collides here instead of enqueueing, so
        # promotion and material change differ only in next_attempt_at.
        # Dead-lettered rows are excluded, or a subject could never be re-enqueued.
        sa.Index(
            "uq_pipeline_work_open_article",
            "article_id",
            unique=True,
            postgresql_where=sa.text("dead_lettered_at IS NULL AND article_id IS NOT NULL"),
        ),
        sa.Index(
            "uq_pipeline_work_open_cluster",
            "cluster_id",
            unique=True,
            postgresql_where=sa.text("dead_lettered_at IS NULL AND cluster_id IS NOT NULL"),
        ),
        # The claim query's access path.
        sa.Index(
            "ix_pipeline_work_claimable",
            "stage",
            "next_attempt_at",
            postgresql_where=sa.text("dead_lettered_at IS NULL"),
        ),
    )

    work_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)

    #: Exactly one of these two is set — article stages vs per-cluster summarization.
    article_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("canonical_record.article_id", ondelete="CASCADE"), nullable=True
    )
    cluster_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("cluster.cluster_id", ondelete="CASCADE"), nullable=True
    )

    stage: Mapped[Stage] = mapped_column(StrEnumType(Stage))

    #: NULL when unclaimed. A claim older than the lease is reclaimable by any worker.
    claimed_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    #: Per-stage, because the row is per-stage — nothing to reset on a transition.
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    #: Backoff, and the debounce window.
    next_attempt_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())

    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Set on terminal failure; the row is kept.
    dead_lettered_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
