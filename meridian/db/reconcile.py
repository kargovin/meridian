"""The work-queue reconciler: hold the queue to what the records say is owed (RFC §6.2, §6.3).

``pipeline_work`` is derived and disposable — the article chain's queue is a pure function of
``pipeline_state`` and ``terminal_reason`` (``owed_stage``). This module compares the two and
repairs the difference, both ways: work the records say is owed and the queue does not hold is
enqueued; work the queue holds and the records do not justify is removed.

An instrument, not a defence. ``advance()`` is the primary path, and a healthy system repairs
nothing, so every count this reports should read zero; a non-zero one is a defect in a stage
handler, logged loudly and repaired rather than absorbed quietly.

Article work only. Summarize work has a cluster subject and derives from
``Cluster.distinct_source_count`` and ``Summary.input_fingerprint`` instead; no summarize
stage exists yet, so there is nothing to derive it against.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from meridian_contract import STAGE_OWED_BY_STATE, PipelineState, Stage, owed_stage
from sqlalchemy.orm import Session

from meridian.db.models import CanonicalRecord, PipelineWork

log = logging.getLogger(__name__)

#: States that still owe an article stage, derived from the chain rather than restated.
_OWING_STATES = [state for state in PipelineState if STAGE_OWED_BY_STATE[state] is not None]


class Kind(StrEnum):
    """How an article's queue disagrees with its record."""

    #: Owes a stage; the queue holds nothing for it. The failure the successor map exists to
    #: prevent — a state that moved without its enqueue.
    MISSING = "missing"
    #: Owes one stage; the queue holds a row for another. Replaced, not added to: one open row
    #: per article is a unique index, so the row to add cannot coexist with the row that is
    #: wrong.
    WRONG_STAGE = "wrong_stage"
    #: Owes nothing — finished, or stopped for good — and still holds a claimable row, which
    #: ``claim()`` would hand to a worker.
    ORPHANED = "orphaned"
    #: Owes a stage and holds only a dead-lettered row: the article was given up without being
    #: marked terminal. Reported and never re-enqueued — re-enqueueing it is the loop
    #: dead-letter → re-enqueue → retry → dead-letter, and it never stops.
    UNMARKED_DEAD_LETTER = "unmarked_dead_letter"


def classify(owed: Stage | None, queued: Stage | None, *, dead_lettered: bool) -> Kind | None:
    """The disagreement between what is owed and what is queued, or ``None`` if they agree."""
    if owed == queued:
        return None
    if queued is None:
        return Kind.UNMARKED_DEAD_LETTER if dead_lettered else Kind.MISSING
    if owed is None:
        return Kind.ORPHANED
    return Kind.WRONG_STAGE


@dataclass(frozen=True)
class Discrepancy:
    """One article whose queue disagrees with its record."""

    article_id: int
    kind: Kind
    #: The stage the record says is owed; ``None`` if nothing is.
    owed: Stage | None
    #: The stage of the open row the queue holds; ``None`` if there is none.
    queued: Stage | None


def survey(session: Session) -> list[Discrepancy]:
    """Every article whose queue disagrees with its record, read in one statement.

    ⚠️ One statement, not two. Read "what is owed" and "what is queued" separately and, under
    READ COMMITTED, each takes its own snapshot even inside one transaction — so a stage
    completing between them is a phantom: the first read sees the article at ``discovered``
    owing acquire, ``advance()`` commits, the second sees the dedup row, and acquire reads as
    missing. A counter that should read zero and cries wolf on a benign race teaches everyone
    to ignore it.

    Reads every article that owes a stage or holds an open row, and classifies them in Python
    with ``owed_stage`` — the same function discovery and the repair use — rather than a second
    copy of the rule in SQL.

    Two branches under one ``UNION``, still one statement and one snapshot. As a single ``OR``
    over the outer-joined work row the planner cannot use the state index and walks every record
    ever ingested — the finished ones are nearly all of them, and the scan grows by a year's
    articles a year. Split, one branch reads the records that owe a stage (indexed on state) and
    the other the records that hold an open row (the queue's size). An article in both appears
    once, because ``UNION`` drops identical rows.
    """
    open_work = (
        sa.select(PipelineWork.article_id, PipelineWork.stage)
        .where(PipelineWork.article_id.is_not(None), PipelineWork.dead_lettered_at.is_(None))
        .subquery("open_work")
    )
    dead = (
        sa.select(PipelineWork.article_id)
        .where(PipelineWork.article_id.is_not(None), PipelineWork.dead_lettered_at.is_not(None))
        .distinct()
        .subquery("dead")
    )
    columns = (
        CanonicalRecord.article_id,
        CanonicalRecord.pipeline_state,
        CanonicalRecord.terminal_reason,
        open_work.c.stage.label("queued"),
        dead.c.article_id.is_not(None).label("dead_lettered"),
    )
    owing = (
        sa.select(*columns)
        .select_from(CanonicalRecord)
        .outerjoin(open_work, open_work.c.article_id == CanonicalRecord.article_id)
        .outerjoin(dead, dead.c.article_id == CanonicalRecord.article_id)
        .where(
            CanonicalRecord.terminal_reason.is_(None),
            CanonicalRecord.pipeline_state.in_(_OWING_STATES),
        )
    )
    holding = (
        sa.select(*columns)
        .select_from(CanonicalRecord)
        .join(open_work, open_work.c.article_id == CanonicalRecord.article_id)
        .outerjoin(dead, dead.c.article_id == CanonicalRecord.article_id)
    )
    both = sa.union(owing, holding).subquery("both")
    rows = session.execute(sa.select(both).order_by(both.c.article_id)).all()
    found = []
    for article_id, state, terminal, queued_value, dead_lettered in rows:
        owed = owed_stage(state, terminal)
        queued = Stage(queued_value) if queued_value is not None else None
        kind = classify(owed, queued, dead_lettered=dead_lettered)
        if kind is not None:
            found.append(Discrepancy(article_id, kind, owed, queued))
    return found


def repair(session: Session, article_id: int) -> Discrepancy | None:
    """Bring one article's queue into line with its record. Commits.

    Re-derives the disagreement from scratch under a lock rather than trusting the survey that
    named this article: the survey was a snapshot, and a stage may have completed since. The
    record is locked ``FOR UPDATE`` first, which conflicts with the lock every writer that ends
    a stage takes on it (``work_queue._lock_article``) — so either their change committed before
    this read and is seen, or they wait for this one.

    ⚠️ Record, then work row: the order every writer that takes both must use
    (``work_queue._lock_article``). The reverse order against this one is a deadlock.

    Returns what was found under the lock, or ``None`` if the article agrees with its queue
    now, or is gone. An ``UNMARKED_DEAD_LETTER`` is returned and not repaired.

    ⚠️ A wrong or orphaned row is deleted and a new one inserted, never updated in place. A
    worker may be holding the old row's claim; ``advance()`` discharges by ``work_id``, so an
    updated row would still be discharged by the worker that claimed it for another stage. A
    deleted one makes that worker's discharge raise ``StaleWork`` instead.
    """
    try:
        record = session.execute(
            sa.select(CanonicalRecord.pipeline_state, CanonicalRecord.terminal_reason)
            .where(CanonicalRecord.article_id == article_id)
            .with_for_update()
        ).one_or_none()
        if record is None:
            session.rollback()
            return None
        owed = owed_stage(record.pipeline_state, record.terminal_reason)
        row = session.execute(
            sa.select(PipelineWork.work_id, PipelineWork.stage)
            .where(PipelineWork.article_id == article_id, PipelineWork.dead_lettered_at.is_(None))
            .with_for_update()
        ).one_or_none()
        queued = row.stage if row is not None else None
        dead_lettered = (
            session.scalar(
                sa.select(PipelineWork.work_id)
                .where(
                    PipelineWork.article_id == article_id,
                    PipelineWork.dead_lettered_at.is_not(None),
                )
                .limit(1)
            )
            is not None
        )
        kind = classify(owed, queued, dead_lettered=dead_lettered)
        if kind is None or kind is Kind.UNMARKED_DEAD_LETTER:
            session.rollback()
            return Discrepancy(article_id, kind, owed, queued) if kind is not None else None

        if row is not None:
            session.execute(sa.delete(PipelineWork).where(PipelineWork.work_id == row.work_id))
        if owed is not None:
            session.execute(sa.insert(PipelineWork).values(article_id=article_id, stage=owed))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return Discrepancy(article_id, kind, owed, queued)


@dataclass(frozen=True)
class StageDepth:
    """How much work one stage holds, and how long the oldest of it has waited."""

    stage: Stage
    #: Open rows: not dead-lettered, whether due, claimed or waiting on backoff.
    open: int
    #: Age of the oldest open row, from when it was enqueued. The instrument for a row that is
    #: released forever: it never leaves the queue and the reconciler reads it as healthy on
    #: every run, but its age grows without bound.
    oldest: dt.timedelta


def depth(session: Session, *, now: dt.datetime | None = None) -> list[StageDepth]:
    """Open rows per stage, in the order the stages are declared. Stages holding none are left
    out. Cluster work is included: its rows sit in the same table."""
    now = now or dt.datetime.now(dt.UTC)
    rows = session.execute(
        sa.select(PipelineWork.stage, sa.func.count(), sa.func.min(PipelineWork.enqueued_at))
        .where(PipelineWork.dead_lettered_at.is_(None))
        .group_by(PipelineWork.stage)
    ).all()
    by_stage = {
        Stage(stage): StageDepth(Stage(stage), count, max(now - oldest, dt.timedelta(0)))
        for stage, count, oldest in rows
    }
    return [by_stage[stage] for stage in Stage if stage in by_stage]


@dataclass(frozen=True)
class ReconcileReport:
    """What one run found, and how much work each stage holds."""

    repaired: Sequence[Discrepancy] = ()
    #: Found and not repaired: an ``UNMARKED_DEAD_LETTER``, as re-derived under the lock, or a
    #: repair that raised — which carries the survey's reading, taken before the lock and
    #: possibly overtaken since. Each is logged where it happened.
    unrepaired: Sequence[Discrepancy] = ()
    depth: Sequence[StageDepth] = ()

    def count(self, kind: Kind) -> int:
        return sum(1 for d in (*self.repaired, *self.unrepaired) if d.kind is kind)

    @property
    def clean(self) -> bool:
        """Nothing disagreed. What a healthy system reports on every run."""
        return not self.repaired and not self.unrepaired


def run(session: Session, *, now: dt.datetime | None = None) -> ReconcileReport:
    """Survey the queue, repair each article found, and measure each stage. Commits.

    One transaction per repaired article, so a repair that fails is rolled back alone and the
    rest still happen.
    """
    candidates = survey(session)
    session.commit()
    repaired: list[Discrepancy] = []
    unrepaired: list[Discrepancy] = []
    for candidate in candidates:
        try:
            outcome = repair(session, candidate.article_id)
        except Exception:
            log.exception("could not repair the queue of article %d", candidate.article_id)
            unrepaired.append(candidate)
            continue
        if outcome is None:
            continue
        if outcome.kind is Kind.UNMARKED_DEAD_LETTER:
            log.error(
                "article %d owes %s and holds only a dead-lettered row, with no terminal_reason; "
                "not re-enqueued — a dead-letter path skipped marking the article",
                outcome.article_id,
                outcome.owed,
            )
            unrepaired.append(outcome)
            continue
        log.warning(
            "article %d: %s (owed %s, queued %s) — repaired. A handler dropped or corrupted "
            "this article's work row; the count should be zero.",
            outcome.article_id,
            outcome.kind,
            outcome.owed,
            outcome.queued,
        )
        repaired.append(outcome)
    stages = depth(session, now=now)
    session.commit()
    return ReconcileReport(repaired=repaired, unrepaired=unrepaired, depth=stages)
