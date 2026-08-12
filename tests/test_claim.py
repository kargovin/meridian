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


def test_claim_marks_the_row(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(session, stage=Stage.CLASSIFY, article=article)
    session.commit()

    (claimed,) = claim(session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE)
    assert claimed.claimed_by == "w1"
    assert claimed.claimed_at is not None
    assert claimed.attempts == 1


def test_concurrent_claims_are_disjoint(session: Session, migrated: sa.Engine) -> None:
    """AC2. Two workers starting together must never receive the same row."""
    source = make_source(session)
    for i in range(10):
        article = make_article(session, source, guid=f"a{i}")
        make_work(session, stage=Stage.CLASSIFY, article=article)
    session.commit()

    start = threading.Barrier(2)
    results: list[set[int]] = []
    guard = threading.Lock()

    def worker(name: str) -> None:
        with Session(migrated, expire_on_commit=False) as db:
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


def test_claim_refuses_a_session_that_expires_on_commit(migrated: sa.Engine) -> None:
    """claim() commits, so such a session would hand back rows that are already expired."""
    with Session(migrated) as db, pytest.raises(ValueError, match="expire_on_commit"):
        claim(db, stage=Stage.CLASSIFY, worker="w1", lease=LEASE)


def test_other_stages_are_left_alone(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(session, stage=Stage.ACQUIRE, article=article)
    session.commit()

    assert claim(session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_rows_due_later_are_not_claimed(session: Session) -> None:
    """Backoff and the debounce window both work by pushing next_attempt_at forward."""
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(
        session,
        stage=Stage.CLASSIFY,
        article=article,
        next_attempt_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10),
    )
    session.commit()

    assert claim(session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_dead_lettered_rows_are_not_claimed(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(
        session,
        stage=Stage.CLASSIFY,
        article=article,
        dead_lettered_at=dt.datetime.now(dt.UTC),
    )
    session.commit()

    assert claim(session, stage=Stage.CLASSIFY, worker="w1", lease=LEASE) == []


def test_a_stale_claim_is_reclaimable(session: Session) -> None:
    """A worker that dies mid-stage must not strand its row."""
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(
        session,
        stage=Stage.CLASSIFY,
        article=article,
        claimed_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=30),
        claimed_by="dead-worker",
        attempts=1,
    )
    session.commit()

    (reclaimed,) = claim(session, stage=Stage.CLASSIFY, worker="w2", lease=LEASE)
    assert reclaimed.claimed_by == "w2"
    assert reclaimed.attempts == 2


def test_a_live_claim_is_not_stolen(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(
        session,
        stage=Stage.CLASSIFY,
        article=article,
        claimed_at=dt.datetime.now(dt.UTC),
        claimed_by="w1",
    )
    session.commit()

    assert claim(session, stage=Stage.CLASSIFY, worker="w2", lease=LEASE) == []
