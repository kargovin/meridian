"""Keeping a claim alive, giving a row back, and giving up on it.

These are the queue's answers to a stage that waits on the network: a batch that outlives
its lease, a publisher that says come back later, and one that never stops saying it.
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract import Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import CanonicalRecord, PipelineWork
from tests.factories import make_article, make_source, make_work

pytestmark = pytest.mark.postgres

LEASE = dt.timedelta(minutes=5)
#: A fixed clock for the tests that drive time by hand; rows are made due at this instant.
T0 = dt.datetime(2026, 9, 18, 12, 0, tzinfo=dt.UTC)


def _claimed_at(session: Session, work_id: int) -> dt.datetime | None:
    return session.scalar(sa.select(PipelineWork.claimed_at).where(PipelineWork.work_id == work_id))


def test_heartbeat_restamps_only_the_rows_this_worker_holds(app_session: Session) -> None:
    """AC5. The half a stolen row is left out of is what makes the result an ownership check."""
    source = make_source(app_session)
    rows = [
        make_work(
            app_session,
            stage=Stage.ACQUIRE,
            article=make_article(app_session, source, guid=g),
            next_attempt_at=T0,
        )
        for g in ("a", "b", "c")
    ]
    app_session.commit()
    t0 = T0
    claimed = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE, limit=3, now=t0
    )
    assert {r.work_id for r in claimed} == {r.work_id for r in rows}

    # Another worker reclaims one row after the lease — the situation a heartbeat must notice.
    stolen = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE, limit=1, now=t0 + LEASE * 2
    )
    assert len(stolen) == 1
    stolen_id = stolen[0].work_id

    later = t0 + dt.timedelta(minutes=4)
    kept = work_queue.heartbeat(app_session, [r.work_id for r in rows], worker="w1", now=later)

    assert kept == {r.work_id for r in rows} - {stolen_id}
    for r in rows:
        stamp = _claimed_at(app_session, r.work_id)
        if r.work_id == stolen_id:
            assert stamp == t0 + LEASE * 2, "the thief's stamp must be left alone"
        else:
            assert stamp == later


def test_heartbeat_does_not_count_as_an_attempt(app_session: Session) -> None:
    source = make_source(app_session)
    work = make_work(
        app_session, stage=Stage.ACQUIRE, article=make_article(app_session, source, guid="a")
    )
    app_session.commit()
    (claimed,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)
    assert claimed.attempts == 1

    for _ in range(50):
        work_queue.heartbeat(app_session, [work.work_id], worker="w1")

    attempts = app_session.scalar(
        sa.select(PipelineWork.attempts).where(PipelineWork.work_id == work.work_id)
    )
    assert attempts == 1


def test_a_heartbeated_row_is_not_reclaimable_after_the_original_lease(
    app_session: Session,
) -> None:
    """AC5. The lease bounds the gap between heartbeats, not the batch."""
    source = make_source(app_session)
    work = make_work(
        app_session,
        stage=Stage.ACQUIRE,
        article=make_article(app_session, source, guid="a"),
        next_attempt_at=T0,
    )
    app_session.commit()
    t0 = T0
    work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE, now=t0)
    work_queue.heartbeat(app_session, [work.work_id], worker="w1", now=t0 + dt.timedelta(minutes=4))

    # Past the original lease, inside the renewed one: nobody else may take it.
    at_six = t0 + dt.timedelta(minutes=6)
    assert (
        work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE, now=at_six)
        == []
    )
    # Past the renewed lease: reclaimable, as a dead worker's row must be.
    at_ten = t0 + dt.timedelta(minutes=10)
    assert (
        len(
            work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE, now=at_ten)
        )
        == 1
    )


def test_release_gives_the_row_back_with_a_future_due_time(app_session: Session) -> None:
    """AC4. A released row is not claimable until its retry time, and keeps its attempt count."""
    source = make_source(app_session)
    work = make_work(
        app_session,
        stage=Stage.ACQUIRE,
        article=make_article(app_session, source, guid="a"),
        next_attempt_at=T0,
    )
    app_session.commit()
    t0 = T0
    (claimed,) = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE, now=t0
    )

    retry_at = t0 + dt.timedelta(minutes=1)
    work_queue.release(app_session, claimed, retry_at=retry_at, error="503 Service Unavailable")
    app_session.commit()

    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None
    assert row.claimed_at is None and row.claimed_by is None
    assert row.next_attempt_at == retry_at
    assert row.attempts == 1
    assert row.last_error == "503 Service Unavailable"

    # Not due yet, whatever the lease says; due once the retry time passes.
    assert (
        work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE, now=t0) == []
    )
    again = work_queue.claim(
        app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE, now=retry_at
    )
    assert [r.work_id for r in again] == [work.work_id]
    assert again[0].attempts == 2


def test_release_of_a_discharged_row_is_stale(app_session: Session) -> None:
    source = make_source(app_session)
    work = make_work(
        app_session, stage=Stage.ACQUIRE, article=make_article(app_session, source, guid="a")
    )
    app_session.commit()
    (claimed,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)
    app_session.execute(sa.delete(PipelineWork).where(PipelineWork.work_id == work.work_id))
    app_session.commit()
    # No expunge: the instance stays in the identity map, as it does in a real batch. The guard
    # has to notice from the database, not from what the session already believes.
    with pytest.raises(work_queue.StaleWork):
        work_queue.release(app_session, claimed, retry_at=dt.datetime.now(dt.UTC), error=None)


def test_release_of_a_row_another_worker_reclaimed_is_stale_and_keeps_their_claim(
    app_session: Session,
) -> None:
    """The row is not gone, it is theirs: our lease expired, they claimed it. A release that
    wrote through our instance would clear *their* claim and hand the row back to the queue
    while they are still working on it."""
    source = make_source(app_session)
    work = make_work(
        app_session, stage=Stage.ACQUIRE, article=make_article(app_session, source, guid="a")
    )
    app_session.commit()
    (ours,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)
    app_session.execute(
        sa.update(PipelineWork)
        .where(PipelineWork.work_id == work.work_id)
        .values(claimed_at=dt.datetime.now(dt.UTC) - 2 * LEASE)
    )
    app_session.commit()
    # Their claim goes through a session of their own — in one session the two claims would
    # be one identity-map instance, and ``ours.claimed_by`` would already read theirs.
    with Session(app_session.get_bind(), expire_on_commit=False) as other:
        (theirs,) = work_queue.claim(other, stage=Stage.ACQUIRE, worker="w2", lease=LEASE)
        assert theirs.work_id == ours.work_id and theirs.claimed_by == "w2"
    assert ours.claimed_by == "w1"

    with pytest.raises(work_queue.StaleWork):
        work_queue.release(
            app_session, ours, retry_at=dt.datetime.now(dt.UTC), error="503", strike=True
        )
    app_session.rollback()
    app_session.expire_all()
    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None and row.claimed_by == "w2" and row.claimed_at is not None
    assert row.attempts == 2 and row.last_error is None


def test_a_non_strike_release_gives_the_attempt_back(app_session: Session) -> None:
    """A pacing wait is not the row's fault. ``claim`` counted it; the release un-counts it, so
    however often a row is paced out, its first real failure still gets the whole schedule."""
    source = make_source(app_session)
    work = make_work(
        app_session, stage=Stage.ACQUIRE, article=make_article(app_session, source, guid="a")
    )
    app_session.commit()
    for _ in range(4):
        (claimed,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)
        work_queue.release(
            app_session,
            claimed,
            retry_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
            error="pacing",
            strike=False,
        )
        app_session.commit()
    app_session.expire_all()
    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None and row.attempts == 0
    assert row.claimed_at is None and row.last_error == "pacing"

    (claimed,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)
    work_queue.release(app_session, claimed, retry_at=dt.datetime.now(dt.UTC), error="503")
    app_session.commit()
    app_session.expire_all()
    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None and row.attempts == 1


@pytest.mark.parametrize(
    ("attempts", "minutes"),
    [(0, 1), (1, 1), (2, 2), (3, 4), (4, 8), (5, 16), (6, 32), (7, 60), (20, 60)],
)
def test_backoff_doubles_and_caps(attempts: int, minutes: int) -> None:
    assert work_queue.backoff_after(attempts) == dt.timedelta(minutes=minutes)


def test_dead_letter_keeps_the_row_and_marks_the_article_terminal(app_session: Session) -> None:
    """AC4. Both writes — the second is what stops the reconciler re-enqueueing the row."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()
    (claimed,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)

    work_queue.dead_letter(app_session, claimed, error="503, five times")
    app_session.commit()
    app_session.expunge_all()

    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None, "the row is the trace; it must survive"
    assert row.dead_lettered_at is not None
    assert row.claimed_at is None and row.claimed_by is None
    assert row.last_error == "503, five times"
    record = app_session.get(CanonicalRecord, article.article_id)
    assert record is not None and record.terminal_reason is TerminalReason.FAILED

    # Neither claimable nor owed: the queue skips it, and the rebuild derivation does not
    # think the article still owes acquire.
    assert work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w2", lease=LEASE) == []
    assert (article.article_id, Stage.ACQUIRE) not in work_queue.expected_article_work(app_session)


@pytest.mark.parametrize("primitive", ["release", "dead_letter"])
def test_a_row_that_was_never_claimed_cannot_be_given_back(
    app_session: Session, primitive: str
) -> None:
    """``claimed_by == None`` compiles to ``IS NULL``, which matches every unclaimed row by id —
    the opposite of an ownership test. A caller that loaded a row rather than claiming it is
    refused before any write: the row keeps ``attempts = 0`` and the article is not touched."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()
    assert work.claimed_by is None

    with pytest.raises(ValueError, match="no claim"):
        if primitive == "release":
            work_queue.release(
                app_session, work, retry_at=dt.datetime.now(dt.UTC), error="x", strike=False
            )
        else:
            work_queue.dead_letter(app_session, work, error="x")
    app_session.rollback()
    app_session.expire_all()
    row = app_session.get(PipelineWork, work.work_id)
    assert row is not None and row.attempts == 0 and row.dead_lettered_at is None
    record = app_session.get(CanonicalRecord, article.article_id)
    assert record is not None and record.terminal_reason is None
