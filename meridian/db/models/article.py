"""Articles and their collapsed duplicates (RFC §5.1)."""

import datetime as dt

import sqlalchemy as sa
from meridian_contract import BodyProvenance, PipelineState, TerminalReason
from meridian_dbkit import Sha256, StrEnumType, TZDateTime, enum_check
from sqlalchemy.orm import Mapped, mapped_column

from meridian.db.base import Base


class CanonicalRecord(Base):
    """One row per distinct article, post-dedup (FR-I4/I5).

    Columns that only exist after a stage runs are nullable; ``pipeline_state`` says which
    of them should be populated.
    """

    __tablename__ = "canonical_record"
    __table_args__ = (
        # Ingest idempotency: re-polling a feed must not duplicate a representative.
        sa.UniqueConstraint("source_id", "guid"),
        sa.UniqueConstraint("url_canonical"),
        # Indexed, NOT unique: exact duplicates must reach the dedup stage so it can write
        # the AlternateCopy row. A unique constraint could only error or DO NOTHING, and
        # neither collapses provenance.
        sa.Index("ix_canonical_record_content_hash", "content_hash"),
        sa.Index("ix_canonical_record_pipeline_state", "pipeline_state"),
        enum_check("body_provenance", BodyProvenance),
        enum_check("pipeline_state", PipelineState),
        enum_check("terminal_reason", TerminalReason),
    )

    article_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(sa.ForeignKey("source.source_id", ondelete="RESTRICT"))

    url_canonical: Mapped[str] = mapped_column(sa.Text)
    guid: Mapped[str] = mapped_column(sa.Text)

    title: Mapped[str] = mapped_column(sa.Text)
    #: NULL until acquire runs, or permanently for a headline-only source.
    body_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    lede: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    published_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
    first_seen_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())

    #: FR-I7 drops non-English; NULL until detection has run.
    language: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    body_provenance: Mapped[BodyProvenance | None] = mapped_column(
        StrEnumType(BodyProvenance), nullable=True
    )
    content_hash: Mapped[str | None] = mapped_column(Sha256, nullable=True)
    #: Unsigned 64-bit stored as signed bigint — see ``types.simhash_to_db``.
    simhash: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)

    #: Legal assumption A-L1.
    retention_expires_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)
    #: Last observed publisher edit; gates re-summarization, not re-classification.
    content_updated_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime, nullable=True)

    pipeline_state: Mapped[PipelineState] = mapped_column(
        StrEnumType(PipelineState),
        server_default=PipelineState.DISCOVERED.value,
    )
    #: NULL = still live.
    terminal_reason: Mapped[TerminalReason | None] = mapped_column(
        StrEnumType(TerminalReason), nullable=True
    )


class AlternateCopy(Base):
    """A duplicate collapsed into a representative, kept for provenance (FR-I5, US-K2).

    Distinct sources for a record is ``1 + count(DISTINCT source_id)`` over these rows —
    counting rows would treat one publisher's reissue as a second source and could promote a
    single-source cluster past the FR-S6 gate.
    """

    __tablename__ = "alternate_copy"
    __table_args__ = (
        # A collapsed duplicate is not in canonical_record, so re-polling does not recognise
        # it. Without these the row count climbs on every poll.
        sa.UniqueConstraint("source_id", "guid"),
        sa.UniqueConstraint("url"),
        sa.Index("ix_alternate_copy_article_id", "article_id"),
    )

    alternate_copy_id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    article_id: Mapped[int] = mapped_column(
        sa.ForeignKey("canonical_record.article_id", ondelete="CASCADE")
    )
    source_id: Mapped[int] = mapped_column(sa.ForeignKey("source.source_id", ondelete="RESTRICT"))
    url: Mapped[str] = mapped_column(sa.Text)
    #: Nullable — tier-3 acquisition has no feed-native id. UNIQUE(url) is what covers those.
    guid: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    seen_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
