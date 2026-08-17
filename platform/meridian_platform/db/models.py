"""The job store.

A queue and a result store at once. The claim shape follows the application's work queue,
the lifecycle does not: rows are not deleted on success, because the result is what the
caller has not read yet, and nothing can rebuild this table.
"""

import datetime as dt
import uuid
from typing import get_args

import sqlalchemy as sa
from meridian_contract.api import JobStatus, WireWithholdReason
from meridian_dbkit import StrEnumType, TZDateTime, enum_check
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from meridian_platform.db.base import Base

_WIRE_WITHHOLD_REASONS = ", ".join(f"'{value}'" for value in get_args(WireWithholdReason))


class SummarizeJob(Base):
    """One accepted ``/v1/summarize`` request.

    ``public_id`` is the only identifier that crosses the wire: a storage key lives as long
    as the row, a published handle lives in the consumer's logs and support tickets.
    """

    __tablename__ = "summarize_job"
    __table_args__ = (
        enum_check("status", JobStatus),
        # NULL keys do not collide, so unkeyed requests are never deduplicated.
        sa.UniqueConstraint("consumer", "idempotency_key"),
        sa.Index(
            "ix_summarize_job_claimable",
            "next_attempt_at",
            postgresql_where=sa.text("status = 'queued'"),
        ),
        sa.Index("ix_summarize_job_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, unique=True, default=uuid.uuid4)

    #: Caller identity, from the request's token. Authorizes the poll.
    consumer: Mapped[str] = mapped_column(sa.String(64))
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(255))

    status: Mapped[JobStatus] = mapped_column(StrEnumType(JobStatus))

    claimed_at: Mapped[dt.datetime | None] = mapped_column(TZDateTime)
    claimed_by: Mapped[str | None] = mapped_column(sa.String(64))
    attempts: Mapped[int] = mapped_column(sa.Integer, server_default="0")
    next_attempt_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    last_error: Mapped[str | None] = mapped_column(sa.Text)

    created_at: Mapped[dt.datetime] = mapped_column(TZDateTime, server_default=sa.func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=sa.func.now(), onupdate=sa.func.now()
    )

    #: When the row itself may be deleted. Also the idempotency-replay window.
    expires_at: Mapped[dt.datetime] = mapped_column(TZDateTime)

    items: Mapped[list["SummarizeJobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class SummarizeJobItem(Base):
    """One item of a batch: its input, and its result or its failure.

    A child row per item rather than one JSON document on the job: PostgreSQL cannot update
    part of a JSON value, so filling results in one at a time would rewrite the whole value
    once per item.

    ``documents`` is set to NULL when the job reaches a terminal state — that discard is the
    published retention guarantee, not housekeeping.
    """

    __tablename__ = "summarize_job_item"
    __table_args__ = (
        sa.UniqueConstraint("job_id", "item_id"),
        sa.CheckConstraint(
            f"withhold_reason IS NULL OR withhold_reason IN ({_WIRE_WITHHOLD_REASONS})",
            name="withhold_reason",
        ),
        sa.CheckConstraint(
            "faithfulness_score IS NULL OR faithfulness_score BETWEEN 0 AND 1",
            name="faithfulness_score_range",
        ),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, sa.Identity(), primary_key=True)
    job_id: Mapped[int] = mapped_column(
        sa.BigInteger, sa.ForeignKey("summarize_job.id", ondelete="CASCADE")
    )

    #: The caller's opaque correlation handle, echoed back unchanged.
    item_id: Mapped[str] = mapped_column(sa.String(255))

    documents: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)

    summary: Mapped[str | None] = mapped_column(sa.Text)
    faithfulness_score: Mapped[float | None] = mapped_column(sa.Float)
    withheld: Mapped[bool | None] = mapped_column(sa.Boolean)
    withhold_reason: Mapped[str | None] = mapped_column(sa.String(64))

    error_code: Mapped[str | None] = mapped_column(sa.String(64))
    error_message: Mapped[str | None] = mapped_column(sa.Text)

    job: Mapped[SummarizeJob] = relationship(back_populates="items")
