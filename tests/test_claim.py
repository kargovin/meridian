"""Claiming work rows.

SKIP LOCKED does not exist on SQLite, so nothing here proves anything without a real
PostgreSQL — a passing in-memory version of these tests would be worthless.
"""

import datetime as dt
import threading

import pytest
import sqlalchemy as sa
from meridian_contract import Stage
from sqlalchemy.orm import Session

from meridian.db.work_queue import claim
from tests.factories import make_article, make_source, make_work

pytestmark = pytest.mark.postgres

LEASE = dt.timedelta(minutes=5)


def test_claim_marks_the_row(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(app_session, stage=Stage.CLASSIFY, article=article)
    app_session.commit()

    (claimed,) = claim(app_session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE)
    assert claimed.claimed_by == "w1"
    assert claimed.claimed_at is not None
    assert claimed.attempts == 1


def test_concurrent_claims_are_disjoint(app_session: Session, app_migrated: sa.Engine) -> None:
    """AC2. Two workers starting together must never receive the same row."""
    source = make_source(app_session)
    for i in range(10):
        article = make_article(app_session, source, guid=f"a{i}")
        make_work(app_session, stage=Stage.CLASSIFY, article=article)
    app_session.commit()

    start = threading.Barrier(2)
    results: list[set[int]] = []
    guard = threading.Lock()

    def worker(name: str) -> None:
        with Session(app_migrated, expire_on_commit=False) as db:
            start.wait(timeout=5)
            rows = claim(db, stage=Stage.CLASSIFY, worker=name, lease=LEASE, limit=5)
            with guard:
                results.append({row.work_id for row in rows})

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2, "a worker failed to report"
    assert results[0] & results[1] == set(), "the same row was claimed twice"
    assert len(results[0]) + len(results[1]) == 10


def test_a_locked_row_does_not_block_another_worker(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """AC2, the half disjointness does not cover.

    Plain ``FOR UPDATE`` also hands out disjoint sets — it gets there by making the second
    worker wait for the first. Skipping rather than waiting is the property SKIP LOCKED
    actually buys, and only this test fails when it is lost.

    ``lock_timeout`` is what turns "blocked" into a failure instead of a hung suite. It is
    set ``LOCAL``, so the commit inside ``claim()`` reverts it and the pooled connection
    does not carry it into the next test.
    """
    source = make_source(app_session)
    now = dt.datetime.now(dt.UTC)
    held = make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=make_article(app_session, source, guid="a"),
        next_attempt_at=now - dt.timedelta(minutes=2),
    )
    free = make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=make_article(app_session, source, guid="b"),
        next_attempt_at=now - dt.timedelta(minutes=1),
    )
    app_session.commit()

    holder = app_migrated.connect()
    try:
        # Ordered oldest-first, so this is the row a claimer reaches before any other.
        holder.execute(
            sa.text("SELECT work_id FROM pipeline_work WHERE work_id = :id FOR UPDATE"),
            {"id": held.work_id},
        )
        with Session(app_migrated, expire_on_commit=False) as db:
            db.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
            claimed = claim(db, stage=Stage.CLASSIFY, worker="w1", lease=LEASE, limit=2)
    finally:
        holder.rollback()
        holder.close()

    assert [row.work_id for row in claimed] == [free.work_id]


def test_claim_refuses_a_session_that_expires_on_commit(app_migrated: sa.Engine) -> None:
    """claim() commits, so such a session would hand back rows that are already expired."""
    with Session(app_migrated) as db, pytest.raises(ValueError, match="expire_on_commit"):
        claim(db, stage=Stage.CLASSIFY, worker="w1", lease=LEASE)


def test_other_stages_are_left_alone(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    assert claim(app_session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_rows_due_later_are_not_claimed(app_session: Session) -> None:
    """Backoff and the debounce window both work by pushing next_attempt_at forward."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=article,
        next_attempt_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
    )
    app_session.commit()

    assert claim(app_session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_dead_lettered_rows_are_not_claimed(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=article,
        dead_lettered_at=dt.datetime.now(dt.UTC),
    )
    app_session.commit()

    assert claim(app_session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_a_stale_claim_is_reclaimable(app_session: Session) -> None:
    """A worker that dies mid-stage must not strand its row."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=article,
        claimed_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30),
        claimed_by="dead-worker",
        attempts=1,
    )
    app_session.commit()

    (reclaimed,) = claim(app_session, stage=Stage.CLASSIFY, worker="w2", lease=LEASE)
    assert reclaimed.claimed_by == "w2"
    assert reclaimed.attempts == 2


def test_a_live_claim_is_not_stolen(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=article,
        claimed_at=dt.datetime.now(dt.UTC),
        claimed_by="w1",
    )
    app_session.commit()

    assert claim(app_session, stage=Stage.CLASSIFY, worker="w2", lease=LEASE) == []
