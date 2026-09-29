"""The work-queue reconciler: the queue is derived from the records, and held to them (RFC §6.3).

Against a real PostgreSQL: the one-open-row-per-article index, ``FOR UPDATE`` and READ
COMMITTED's per-statement snapshots are all load-bearing here.
"""

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage, TerminalReason
from sqlalchemy import event
from sqlalchemy.orm import Session

from meridian.db import reconcile, work_queue
from meridian.db.models import PipelineWork
from meridian.db.reconcile import Discrepancy, Kind
from meridian.db.session import session_factory
from tests.factories import make_article, make_cluster, make_source, make_work

pytestmark = pytest.mark.postgres

LEASE = dt.timedelta(minutes=5)


def _open(session: Session) -> set[tuple[int, Stage]]:
    """``(article_id, stage)`` for every open article row, read fresh."""
    session.expire_all()
    rows = session.execute(
        sa.select(PipelineWork.article_id, PipelineWork.stage).where(
            PipelineWork.article_id.is_not(None), PipelineWork.dead_lettered_at.is_(None)
        )
    ).all()
    return {(article_id, stage) for article_id, stage in rows if article_id is not None}


def _rows_of(session: Session, article_id: int) -> list[PipelineWork]:
    session.expire_all()
    return list(
        session.scalars(sa.select(PipelineWork).where(PipelineWork.article_id == article_id))
    )


@pytest.fixture
def populated(app_session: Session) -> dict[str, int]:
    """One article at each state with the work it owes, plus a finished and a dropped one."""
    source = make_source(app_session)
    ids = {}
    for label, state, stage in [
        ("discovered", PipelineState.DISCOVERED, Stage.ACQUIRE),
        ("acquired", PipelineState.ACQUIRED, Stage.DEDUP),
        ("deduped", PipelineState.DEDUPED, Stage.CLASSIFY),
        ("classified", PipelineState.CLASSIFIED, Stage.CLUSTER),
    ]:
        article = make_article(app_session, source, guid=label, state=state)
        make_work(app_session, stage=stage, article=article)
        ids[label] = article.article_id

    ids["clustered"] = make_article(
        app_session, source, guid="clustered", state=PipelineState.CLUSTERED
    ).article_id
    ids["dropped"] = make_article(
        app_session,
        source,
        guid="dropped",
        state=PipelineState.DISCOVERED,
        terminal_reason=TerminalReason.DROPPED_LANGUAGE,
    ).article_id
    app_session.commit()
    return ids


# --------------------------------------------------------------------------- the survey


def test_a_healthy_queue_disagrees_with_nothing(
    app_session: Session, populated: dict[str, int]
) -> None:
    assert reconcile.survey(app_session) == []
    assert reconcile.run(app_session).clean


@pytest.mark.parametrize(
    ("owed", "queued", "dead_lettered", "kind"),
    [
        (Stage.DEDUP, Stage.DEDUP, False, None),
        (None, None, False, None),
        (None, None, True, None),
        (Stage.DEDUP, None, False, Kind.MISSING),
        (Stage.DEDUP, None, True, Kind.UNMARKED_DEAD_LETTER),
        (Stage.DEDUP, Stage.ACQUIRE, False, Kind.WRONG_STAGE),
        (Stage.DEDUP, Stage.ACQUIRE, True, Kind.WRONG_STAGE),
        (None, Stage.ACQUIRE, False, Kind.ORPHANED),
    ],
)
def test_classify(
    owed: Stage | None, queued: Stage | None, dead_lettered: bool, kind: Kind | None
) -> None:
    assert reconcile.classify(owed, queued, dead_lettered=dead_lettered) is kind


def test_the_survey_reads_in_one_snapshot(
    app_session: Session, app_migrated: sa.Engine, populated: dict[str, int]
) -> None:
    """⚠️ A stage completing while the survey runs is not a discrepancy. Read what is owed and
    what is queued in two statements and, under READ COMMITTED, each sees its own snapshot: the
    first sees the article owing acquire, ``advance()`` commits, the second sees the dedup row,
    and acquire reads as missing — a false alarm on the counter that should always read zero.

    The advance is committed from another session the moment the survey's first statement has
    run, which is the one interleaving a two-statement survey cannot survive.
    """
    armed = True

    def advance_elsewhere(*_: Any) -> None:
        nonlocal armed
        if not armed:
            return
        armed = False
        with session_factory(app_migrated)() as other:
            (work,) = work_queue.claim(other, stage=Stage.ACQUIRE, worker="w", lease=LEASE)
            work_queue.advance(other, work)
            other.commit()

    event.listen(app_migrated, "after_cursor_execute", advance_elsewhere)
    try:
        found = reconcile.survey(app_session)
    finally:
        event.remove(app_migrated, "after_cursor_execute", advance_elsewhere)

    assert not armed, "the concurrent advance never ran"
    assert found == []
    assert (populated["discovered"], Stage.DEDUP) in _open(app_session)


def test_a_dead_lettered_row_on_a_terminal_article_is_not_a_discrepancy(
    app_session: Session,
) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, terminal_reason=TerminalReason.FAILED)
    make_work(app_session, stage=Stage.ACQUIRE, article=article, dead_lettered_at=sa.func.now())
    app_session.commit()

    assert reconcile.survey(app_session) == []


def test_cluster_work_is_not_surveyed(app_session: Session) -> None:
    """Summarize work has a cluster subject and a derivation of its own; an article survey that
    counted it would report every summarize row as orphaned."""
    make_work(app_session, stage=Stage.SUMMARIZE, cluster=make_cluster(app_session))
    app_session.commit()

    assert reconcile.survey(app_session) == []


# --------------------------------------------------------------------------- the repairs


def test_the_queue_survives_being_truncated(
    app_session: Session, populated: dict[str, int]
) -> None:
    """RFC §6.2's rebuildability claim, exercised: throw the queue away and get it back."""
    before = _open(app_session)
    app_session.execute(sa.text("TRUNCATE pipeline_work RESTART IDENTITY"))
    app_session.commit()

    report = reconcile.run(app_session)

    assert _open(app_session) == before
    assert report.count(Kind.MISSING) == len(before) == 4
    assert reconcile.run(app_session).clean


def test_a_dropped_enqueue_is_put_back(app_session: Session, populated: dict[str, int]) -> None:
    """The failure the successor map exists to prevent: state advanced, nothing enqueued."""
    app_session.execute(
        sa.delete(PipelineWork).where(PipelineWork.article_id == populated["acquired"])
    )
    app_session.commit()

    report = reconcile.run(app_session)

    assert list(report.repaired) == [
        Discrepancy(populated["acquired"], Kind.MISSING, Stage.DEDUP, None)
    ]
    assert (populated["acquired"], Stage.DEDUP) in _open(app_session)
    assert reconcile.run(app_session).clean


def test_a_wrong_stage_row_is_replaced_not_added_to(app_session: Session) -> None:
    """⚠️ One open row per article is a unique index, so inserting the owed row beside the wrong
    one raises. The survey says re-enqueue; the index says replace."""
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.ACQUIRED)
    stale = make_work(app_session, stage=Stage.CLUSTER, article=article)
    app_session.commit()

    report = reconcile.run(app_session)

    assert list(report.repaired) == [
        Discrepancy(article.article_id, Kind.WRONG_STAGE, Stage.DEDUP, Stage.CLUSTER)
    ]
    (row,) = _rows_of(app_session, article.article_id)
    assert row.stage is Stage.DEDUP
    assert row.work_id != stale.work_id
    assert row.attempts == 0 and row.claimed_by is None


def test_a_replaced_row_cannot_be_completed_by_the_worker_that_held_it(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """⚠️ Replaced by delete-and-insert, never an update in place. ``advance()`` discharges by
    ``work_id``; a row updated to the owed stage would still be discharged by the worker holding
    its claim for the wrong one, moving the article on a stage it did not run."""
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.ACQUIRED)
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()
    (held,) = work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w1", lease=LEASE)

    with session_factory(app_migrated)() as other:
        reconcile.run(other)

    with pytest.raises(work_queue.StaleWork):
        work_queue.advance(app_session, held)
    app_session.rollback()
    (row,) = _rows_of(app_session, article.article_id)
    assert row.stage is Stage.DEDUP


@pytest.mark.parametrize(
    ("state", "terminal"),
    [
        (PipelineState.DISCOVERED, TerminalReason.DROPPED_LANGUAGE),
        (PipelineState.CLUSTERED, None),
    ],
    ids=["stopped", "finished"],
)
def test_an_orphaned_row_is_removed(
    app_session: Session, state: PipelineState, terminal: TerminalReason | None
) -> None:
    """Work the records do not justify is otherwise handed to a worker by ``claim()``."""
    source = make_source(app_session)
    article = make_article(app_session, source, state=state, terminal_reason=terminal)
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    report = reconcile.run(app_session)

    assert report.count(Kind.ORPHANED) == 1
    assert _rows_of(app_session, article.article_id) == []
    assert work_queue.claim(app_session, stage=Stage.ACQUIRE, worker="w", lease=LEASE) == []


def test_an_unmarked_dead_letter_is_reported_and_never_re_enqueued(app_session: Session) -> None:
    """⚠️ A dead-lettered row on an article with no ``terminal_reason`` reads as work owed and not
    queued, and the open-row index would accept a new row. Re-enqueueing it is the loop
    dead-letter → re-enqueue → retry → dead-letter, and it never stops."""
    source = make_source(app_session)
    article = make_article(app_session, source)
    make_work(app_session, stage=Stage.ACQUIRE, article=article, dead_lettered_at=sa.func.now())
    app_session.commit()

    for _ in range(2):
        report = reconcile.run(app_session)
        assert list(report.unrepaired) == [
            Discrepancy(article.article_id, Kind.UNMARKED_DEAD_LETTER, Stage.ACQUIRE, None)
        ]
        assert report.repaired == []
    (row,) = _rows_of(app_session, article.article_id)
    assert row.dead_lettered_at is not None


def test_a_repair_re_reads_the_article_under_its_lock(app_session: Session) -> None:
    """The survey is a snapshot; by the time an article is repaired its queue may be right again.
    A repair acting on the survey's word would insert a second open row and fail on the index —
    or, for a stage that completed meanwhile, enqueue work already done."""
    source = make_source(app_session)
    article = make_article(app_session, source)
    app_session.commit()
    (candidate,) = reconcile.survey(app_session)
    assert candidate.kind is Kind.MISSING
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    assert reconcile.repair(app_session, article.article_id) is None
    assert len(_rows_of(app_session, article.article_id)) == 1


def test_one_failed_repair_does_not_stop_the_rest(
    app_session: Session, populated: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    app_session.execute(sa.text("TRUNCATE pipeline_work RESTART IDENTITY"))
    app_session.commit()
    real = reconcile.repair

    def fail_one(session: Session, article_id: int) -> Discrepancy | None:
        if article_id == populated["acquired"]:
            raise RuntimeError("boom")
        return real(session, article_id)

    monkeypatch.setattr(reconcile, "repair", fail_one)
    report = reconcile.run(app_session)

    assert [d.article_id for d in report.unrepaired] == [populated["acquired"]]
    assert report.count(Kind.MISSING) == 4
    assert len(report.repaired) == 3
    assert (populated["deduped"], Stage.CLASSIFY) in _open(app_session)


# --------------------------------------------------------------------------- depth


T0 = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)


def test_depth_counts_open_rows_per_stage_and_ages_the_oldest(app_session: Session) -> None:
    source = make_source(app_session)
    for guid, stage, age in [
        ("a", Stage.ACQUIRE, 10),
        ("b", Stage.ACQUIRE, 90),
        ("c", Stage.CLASSIFY, 30),
    ]:
        article = make_article(app_session, source, guid=guid)
        make_work(
            app_session,
            stage=stage,
            article=article,
            enqueued_at=T0 - dt.timedelta(minutes=age),
        )
    dead = make_article(app_session, source, guid="dead", terminal_reason=TerminalReason.FAILED)
    make_work(
        app_session,
        stage=Stage.DEDUP,
        article=dead,
        dead_lettered_at=T0,
        enqueued_at=T0 - dt.timedelta(days=9),
    )
    app_session.commit()

    assert reconcile.depth(app_session, now=T0) == [
        reconcile.StageDepth(Stage.ACQUIRE, 2, dt.timedelta(minutes=90)),
        reconcile.StageDepth(Stage.CLASSIFY, 1, dt.timedelta(minutes=30)),
    ]


def test_a_row_released_forever_still_ages(app_session: Session) -> None:
    """⚠️ The instrument for a row that never leaves the queue: it is owed and open on every run,
    so the survey reads it as healthy. Its age is measured from when it was enqueued, which a
    release does not move — ``next_attempt_at`` and ``claimed_at`` both do."""
    source = make_source(app_session)
    article = make_article(app_session, source)
    make_work(
        app_session,
        stage=Stage.ACQUIRE,
        article=article,
        next_attempt_at=T0 - dt.timedelta(hours=6),
        enqueued_at=T0 - dt.timedelta(hours=6),
    )
    app_session.commit()
    for hour in range(5, 0, -1):
        now = T0 - dt.timedelta(hours=hour)
        (row,) = work_queue.claim(
            app_session, stage=Stage.ACQUIRE, worker="w", lease=LEASE, now=now
        )
        work_queue.release(app_session, row, retry_at=now, error="robots outage", strike=False)
        app_session.commit()

    (depth,) = reconcile.depth(app_session, now=T0)
    assert depth.oldest == dt.timedelta(hours=6)
    assert reconcile.survey(app_session) == []


def test_enqueued_at_is_stamped_on_insert(app_session: Session) -> None:
    source = make_source(app_session)
    before = app_session.scalar(sa.select(sa.func.now()))
    app_session.commit()
    article = make_article(app_session, source)
    app_session.add(PipelineWork(article_id=article.article_id, stage=Stage.ACQUIRE))
    app_session.commit()

    (row,) = _rows_of(app_session, article.article_id)
    assert before is not None and row.enqueued_at >= before
