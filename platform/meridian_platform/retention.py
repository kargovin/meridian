"""Deleting what we have promised not to keep.

``now`` is an argument rather than something this module reads, so the 24-hour rule can be
tested in milliseconds.

The two databases share a volume that cannot be grown and PostgreSQL has no per-database
size limit, so a sweeper that stops running fills the disk and takes the application's
database down with it. ``overdue`` is the count that says whether that is happening.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Any, cast

import sqlalchemy as sa
from meridian_contract.api import TERMINAL_JOB_STATUSES
from sqlalchemy import CursorResult
from sqlalchemy.orm import Session

from meridian_platform.db import SummarizeJob, SummarizeJobItem


@dataclass(frozen=True)
class SweepResult:
    inputs_discarded: int
    jobs_deleted: int
    #: Rows still past their window after the sweep. Should be zero.
    overdue: int


def sweep(session: Session, now: dt.datetime) -> SweepResult:
    terminal = [status.value for status in TERMINAL_JOB_STATUSES]

    discarded = cast(
        CursorResult[Any],
        session.execute(
            sa.update(SummarizeJobItem)
            .where(
                SummarizeJobItem.documents.isnot(None),
                SummarizeJobItem.job_id.in_(
                    sa.select(SummarizeJob.id).where(SummarizeJob.status.in_(terminal))
                ),
            )
            .values(documents=None)
        ),
    ).rowcount

    deleted = cast(
        CursorResult[Any],
        session.execute(sa.delete(SummarizeJob).where(SummarizeJob.expires_at <= now)),
    ).rowcount

    session.commit()

    overdue = session.scalar(
        sa.select(sa.func.count()).select_from(SummarizeJob).where(SummarizeJob.expires_at <= now)
    )
    return SweepResult(inputs_discarded=discarded, jobs_deleted=deleted, overdue=overdue or 0)
