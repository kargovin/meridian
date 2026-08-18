"""The job path: creation, work, partial outcomes, and who may read a job."""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract.api import JobStatus, SourceDocument, SummarizeItem
from meridian_platform.db import SummarizeJob
from meridian_platform.jobs import RETENTION, claim, enqueue, process_next, read_job, run_sync
from meridian_platform.stub import OVERSIZED, THIN_INPUT
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres

GOOD = "x" * (THIN_INPUT + 100)
TOO_BIG = "x" * (OVERSIZED + 1)


def item(item_id: str, text: str = GOOD) -> SummarizeItem:
    return SummarizeItem(
        id=item_id,
        documents=[SourceDocument(source="outlet-a", title="t", text=text, url="https://e/1")],
    )


def test_a_queued_job_holds_its_input_until_it_runs(platform_session: Session) -> None:
    job = enqueue(platform_session, "digest", [item("c1")])

    assert job.status == JobStatus.QUEUED
    assert job.items[0].documents is not None


def test_processing_fills_results_and_discards_the_input(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])

    assert process_next(platform_session) is True

    job = platform_session.scalars(sa.select(SummarizeJob)).one()
    assert job.status == JobStatus.SUCCEEDED
    assert job.items[0].summary
    assert job.items[0].documents is None


def test_process_next_reports_when_there_is_nothing_to_do(platform_session: Session) -> None:
    assert process_next(platform_session) is False


def test_a_mixed_batch_is_partial(platform_session: Session) -> None:
    """Collapsing it into succeeded or failed loses exactly what per-item reporting is for."""
    enqueue(platform_session, "digest", [item("c1"), item("c2", TOO_BIG)])
    process_next(platform_session)

    state = read_job(
        platform_session,
        str(platform_session.scalars(sa.select(SummarizeJob)).one().public_id),
        "digest",
    )

    assert state is not None
    assert state.status == JobStatus.PARTIAL
    assert [result.id for result in state.results] == ["c1"]
    assert [error.item_id for error in state.errors] == ["c2"]


def test_a_batch_that_fails_entirely_is_failed(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1", TOO_BIG)])
    process_next(platform_session)

    job = platform_session.scalars(sa.select(SummarizeJob)).one()
    assert job.status == JobStatus.FAILED


def test_a_second_worker_does_not_take_a_claimed_job(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])

    assert claim(platform_session, "worker-a") is not None
    assert claim(platform_session, "worker-b") is None


def test_a_lapsed_claim_is_taken_again(platform_session: Session) -> None:
    """A pod dying mid-job must not strand the caller's request forever."""
    enqueue(platform_session, "digest", [item("c1")])
    now = dt.datetime.now(dt.UTC)

    claim(platform_session, "worker-a", now=now)
    retaken = claim(platform_session, "worker-b", now=now + dt.timedelta(hours=1))

    assert retaken is not None
    assert retaken.claimed_by == "worker-b"
    assert retaken.attempts == 2


def test_the_sync_path_also_records_a_job(platform_session: Session) -> None:
    """Sync and async differ in response shape, not in whether anything is stored."""
    job = run_sync(platform_session, "digest", [item("c1")])

    assert job.status == JobStatus.SUCCEEDED
    assert platform_session.scalars(sa.select(SummarizeJob)).one().id == job.id


def test_a_finished_sync_job_is_never_visible_as_queued(platform_session: Session) -> None:
    """It is created and run in one transaction, so no worker can run it a second time."""
    run_sync(platform_session, "digest", [item("c1")])

    assert process_next(platform_session) is False


def test_a_repeated_idempotency_key_returns_the_same_job(platform_session: Session) -> None:
    first = enqueue(platform_session, "digest", [item("c1")], idempotency_key="batch-42")
    second = enqueue(platform_session, "digest", [item("c1")], idempotency_key="batch-42")

    assert first.id == second.id
    assert platform_session.scalars(sa.select(sa.func.count()).select_from(SummarizeJob)).one() == 1


def test_the_same_key_from_another_consumer_is_a_different_job(platform_session: Session) -> None:
    digest = enqueue(platform_session, "digest", [item("c1")], idempotency_key="batch-42")
    meridian = enqueue(platform_session, "meridian", [item("c1")], idempotency_key="batch-42")

    assert digest.id != meridian.id


def test_unkeyed_requests_are_never_deduplicated(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])
    enqueue(platform_session, "digest", [item("c1")])

    assert platform_session.scalars(sa.select(sa.func.count()).select_from(SummarizeJob)).one() == 2


def test_a_caller_cannot_read_another_consumers_job(platform_session: Session) -> None:
    job = enqueue(platform_session, "digest", [item("c1")])

    assert read_job(platform_session, str(job.public_id), "meridian") is None


def test_an_unknown_handle_reads_the_same_as_someone_elses(platform_session: Session) -> None:
    """Both are None, so the endpoint cannot be used to discover which ids are real."""
    job = enqueue(platform_session, "digest", [item("c1")])

    assert read_job(platform_session, str(job.public_id), "meridian") is None
    assert read_job(platform_session, "3f1a5f1e-0000-4000-8000-000000000000", "meridian") is None
    assert read_job(platform_session, "not-a-uuid", "digest") is None


def test_a_job_past_its_window_reads_as_expired(platform_session: Session) -> None:
    """Truthful in the gap between ageing out and being swept, and returns no results."""
    job = enqueue(platform_session, "digest", [item("c1")])
    process_next(platform_session)

    state = read_job(
        platform_session,
        str(job.public_id),
        "digest",
        now=dt.datetime.now(dt.UTC) + RETENTION + dt.timedelta(minutes=1),
    )

    assert state is not None
    assert state.status == JobStatus.EXPIRED
    assert state.results == []


def test_a_claimed_job_reads_as_running(platform_session: Session) -> None:
    """The one status a caller sees while waiting, and the reason polling terminates."""
    job = enqueue(platform_session, "digest", [item("c1")])
    claim(platform_session, "worker-a")

    state = read_job(platform_session, str(job.public_id), "digest")

    assert state is not None
    assert state.status == JobStatus.RUNNING
    assert state.results == []


def test_the_retention_window_starts_when_the_job_finishes(platform_session: Session) -> None:
    """The published guarantee is 24h after terminal state, so a queued job cannot burn it."""
    created = dt.datetime.now(dt.UTC)
    job = enqueue(platform_session, "digest", [item("c1")], now=created)
    finished = created + dt.timedelta(hours=20)

    process_next(platform_session, now=finished)

    assert job.expires_at >= finished + RETENTION - dt.timedelta(seconds=1)


def test_an_expired_job_is_not_a_replay_target(platform_session: Session) -> None:
    """Returning it hands the caller a handle that reads expired and never carries results."""
    created = dt.datetime.now(dt.UTC)
    first = enqueue(platform_session, "digest", [item("c1")], idempotency_key="k", now=created)
    later = created + RETENTION + dt.timedelta(minutes=1)

    second = enqueue(platform_session, "digest", [item("c1")], idempotency_key="k", now=later)

    assert second.id != first.id
