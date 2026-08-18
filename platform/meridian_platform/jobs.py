"""Creating, running and reading summarize jobs.

``process_next`` does one job and returns; the loop that calls it repeatedly lives in
``worker``. Keeping those apart is what lets the work be tested without threads or sleeps.
"""

import datetime as dt
import uuid

import sqlalchemy as sa
from meridian_contract.api import (
    ErrorCode,
    ErrorDetail,
    JobState,
    JobStatus,
    SummarizeItem,
    SummarizeResponse,
    Summary,
)
from sqlalchemy.orm import Session

from meridian_platform.db import SummarizeJob, SummarizeJobItem
from meridian_platform.stub import summarize_documents

#: How long a job and its idempotency key survive after it finishes. One clock for both,
#: so a replayed key can never outlive the job it names. Set at creation as a floor, then
#: re-anchored to the terminal state, so a caller always has the full window to read a
#: result however long the job waited in the queue.
RETENTION = dt.timedelta(hours=24)

#: A claim older than this is treated as abandoned — a pod died mid-job.
LEASE = dt.timedelta(minutes=5)


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _build(
    consumer: str,
    items: list[SummarizeItem],
    idempotency_key: str | None,
    now: dt.datetime,
    status: JobStatus,
) -> SummarizeJob:
    return SummarizeJob(
        public_id=uuid.uuid4(),
        consumer=consumer,
        idempotency_key=idempotency_key,
        status=status,
        expires_at=now + RETENTION,
        items=[
            SummarizeJobItem(
                item_id=item.id,
                documents=[document.model_dump() for document in item.documents],
            )
            for item in items
        ],
    )


def _by_key(
    session: Session, consumer: str, idempotency_key: str, now: dt.datetime
) -> SummarizeJob | None:
    """The live job for this key, releasing a dead one that still holds it.

    A job past its window is not a replay target: returning it hands the caller a handle
    that reads ``expired`` and never carries their results. Deleting it here rather than
    waiting for the sweeper matters because the key is unique — left in place it makes the
    insert collide, and the caller receives the dead job anyway.
    """
    job = session.scalars(
        sa.select(SummarizeJob).where(
            SummarizeJob.consumer == consumer,
            SummarizeJob.idempotency_key == idempotency_key,
        )
    ).one_or_none()

    if job is None:
        return None
    if job.expires_at > now:
        return job

    session.delete(job)
    session.flush()
    return None


def _commit_or_recover(
    session: Session, job: SummarizeJob, consumer: str, idempotency_key: str | None
) -> SummarizeJob:
    """Commit, or return the job that won the race for this idempotency key.

    Two retries arriving together both look the key up, both find nothing, and both insert.
    The unique constraint decides; the loser re-reads instead of creating a second job.
    """
    try:
        session.commit()
    except sa.exc.IntegrityError:
        session.rollback()
        if idempotency_key is None:
            raise
        winner = _by_key(session, consumer, idempotency_key, now_utc())
        if winner is None:
            raise
        return winner
    return job


def _run(job: SummarizeJob, now: dt.datetime | None = None) -> None:
    """Fill in every item and set the job's terminal status. Does not commit."""
    now = now or now_utc()
    failures = 0
    for item in job.items:
        result = summarize_documents(item.documents or [])
        if result.error is not None:
            item.error_code = result.error.code
            item.error_message = result.error.message
            failures += 1
        else:
            item.summary = result.summary
            item.faithfulness_score = result.faithfulness_score
            item.withheld = result.withheld
            item.withhold_reason = result.withhold_reason
        # Discarded the moment it is no longer needed, rather than left for a later sweep.
        item.documents = None

    job.status = _outcome(failures, len(job.items))
    job.expires_at = now + RETENTION
    job.claimed_at = None
    job.claimed_by = None


def _outcome(failures: int, total: int) -> JobStatus:
    """A mixed batch is neither succeeded nor failed, and collapsing it loses the difference."""
    if failures == 0:
        return JobStatus.SUCCEEDED
    if failures == total:
        return JobStatus.FAILED
    return JobStatus.PARTIAL


def enqueue(
    session: Session,
    consumer: str,
    items: list[SummarizeItem],
    idempotency_key: str | None = None,
    now: dt.datetime | None = None,
) -> SummarizeJob:
    """Record a batch for a worker to pick up. The 202 path."""
    now = now or now_utc()

    if idempotency_key is not None:
        existing = _by_key(session, consumer, idempotency_key, now)
        if existing is not None:
            return existing

    job = _build(consumer, items, idempotency_key, now, JobStatus.QUEUED)
    session.add(job)
    return _commit_or_recover(session, job, consumer, idempotency_key)


def run_sync(
    session: Session,
    consumer: str,
    items: list[SummarizeItem],
    idempotency_key: str | None = None,
    now: dt.datetime | None = None,
) -> SummarizeJob:
    """Record and run a batch in one transaction. The 200 path.

    The row is created and finished before it is committed, so no worker can see it queued
    and run it a second time. Sync and async differ in response shape, not in storage.
    """
    now = now or now_utc()

    if idempotency_key is not None:
        existing = _by_key(session, consumer, idempotency_key, now)
        if existing is not None:
            return existing

    job = _build(consumer, items, idempotency_key, now, JobStatus.RUNNING)
    session.add(job)
    _run(job, now)
    return _commit_or_recover(session, job, consumer, idempotency_key)


def claim(session: Session, worker: str, now: dt.datetime | None = None) -> SummarizeJob | None:
    """Take one queued job, or one whose lease has lapsed.

    ``SKIP LOCKED`` so a second worker steps over a row already being claimed rather than
    queueing behind it. Claim-and-commit: the mark is committed before the work starts, so a
    slow job does not hold a write lock for its duration.
    """
    now = now or now_utc()

    job = session.scalars(
        sa.select(SummarizeJob)
        .where(
            sa.or_(
                SummarizeJob.status == JobStatus.QUEUED,
                sa.and_(
                    SummarizeJob.status == JobStatus.RUNNING,
                    SummarizeJob.claimed_at < now - LEASE,
                ),
            ),
            SummarizeJob.next_attempt_at <= now,
        )
        .order_by(SummarizeJob.next_attempt_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).one_or_none()

    if job is None:
        return None

    job.status = JobStatus.RUNNING
    job.claimed_at = now
    job.claimed_by = worker
    job.attempts += 1
    session.commit()
    return job


def process_next(
    session: Session, worker: str = "in-process", now: dt.datetime | None = None
) -> bool:
    """Do one job. Returns False when there was nothing to do."""
    now = now or now_utc()
    job = claim(session, worker, now=now)
    if job is None:
        return False

    _run(job, now)
    session.commit()
    return True


def as_response(job: SummarizeJob) -> SummarizeResponse:
    return SummarizeResponse(results=_results(job), errors=_errors(job))


def read_job(
    session: Session, public_id: str, consumer: str, now: dt.datetime | None = None
) -> JobState | None:
    """The caller's own job, or None.

    None covers three cases deliberately — never existed, already swept, belongs to someone
    else — so the endpoint cannot be used to discover which job ids are real.
    """
    now = now or now_utc()

    try:
        handle = uuid.UUID(public_id)
    except ValueError:
        return None

    job = session.scalars(
        sa.select(SummarizeJob).where(SummarizeJob.public_id == handle)
    ).one_or_none()

    if job is None or job.consumer != consumer:
        return None

    if now >= job.expires_at:
        # Past its window but not yet swept. Returning the results here would hand back data
        # we have published a guarantee to have discarded.
        return JobState(job_id=public_id, status=JobStatus.EXPIRED)

    return JobState(job_id=public_id, status=job.status, results=_results(job), errors=_errors(job))


def _results(job: SummarizeJob) -> list[Summary]:
    return [
        Summary(
            id=item.item_id,
            summary=item.summary or "",
            faithfulness_score=item.faithfulness_score or 0.0,
            withheld=bool(item.withheld),
            withhold_reason=item.withhold_reason,  # type: ignore[arg-type]
        )
        for item in job.items
        if item.error_code is None and item.withheld is not None
    ]


def _errors(job: SummarizeJob) -> list[ErrorDetail]:
    return [
        ErrorDetail(
            code=ErrorCode(item.error_code),
            message=item.error_message or "",
            item_id=item.item_id,
        )
        for item in job.items
        if item.error_code is not None
    ]
