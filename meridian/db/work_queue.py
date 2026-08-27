"""Claiming work rows, and deriving what the queue should hold (RFC §6.2)."""

import datetime as dt
from collections.abc import Sequence

import sqlalchemy as sa
from meridian_contract import (
    ARTICLE_CHAIN,
    PROJECTABLE_STATE,
    STAGE_OWED_BY_STATE,
    STAGE_SUCCESSOR,
    STATE_AFTER_STAGE,
    PipelineState,
    Stage,
    TerminalReason,
    owed_stage,
)
from sqlalchemy.orm import Session

from meridian.db.models import CanonicalRecord, PipelineWork
from meridian.readmodel.project import project_article_cluster, project_cluster

#: States that still owe an article stage — the filter for the rebuild derivation.
_OWING_STATES = [state for state in PipelineState if STAGE_OWED_BY_STATE[state] is not None]


def claim(
    session: Session,
    *,
    stage: Stage,
    worker: str,
    lease: dt.timedelta,
    limit: int = 1,
    now: dt.datetime | None = None,
) -> Sequence[PipelineWork]:
    """Claim up to ``limit`` due rows for ``stage``, and commit.

    Claim-and-commit: the transaction ends here, before the work starts. Do not wrap this in
    an outer transaction that stays open for the duration of the stage — a two-minute
    summarize would hold a write lock for two minutes.

    ``SKIP LOCKED`` is what makes concurrent workers get disjoint sets. It does not exist on
    SQLite, so tests covering this need a real PostgreSQL.

    A row whose claim is older than ``lease`` is treated as unclaimed and taken again, so a
    worker that dies mid-stage does not strand its row.

    The session must not expire on commit — see ``session_factory``.
    """
    if session.expire_on_commit:
        raise ValueError(
            "claim() commits, so a session with expire_on_commit=True hands back rows that "
            "are already expired; the first attribute access re-queries and raises once the "
            "row is deleted. Build sessions with meridian.db.session.session_factory()."
        )
    now = now or dt.datetime.now(dt.UTC)
    due = (
        sa.select(PipelineWork.work_id)
        .where(
            PipelineWork.stage == stage,
            PipelineWork.dead_lettered_at.is_(None),
            PipelineWork.next_attempt_at <= now,
            sa.or_(
                PipelineWork.claimed_at.is_(None),
                PipelineWork.claimed_at < now - lease,
            ),
        )
        .order_by(PipelineWork.next_attempt_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = (
        session.execute(
            sa.update(PipelineWork)
            .where(PipelineWork.work_id.in_(due))
            .values(
                claimed_at=now,
                claimed_by=worker,
                attempts=PipelineWork.attempts + 1,
            )
            .returning(PipelineWork),
            execution_options={"synchronize_session": False},
        )
        .scalars()
        .all()
    )
    session.commit()
    return claimed


def expected_article_work(session: Session) -> set[tuple[int, Stage]]:
    """``(article_id, stage)`` pairs the records say are owed.

    Computed from ``pipeline_state`` and ``terminal_reason`` alone, which is what lets
    ``pipeline_work`` be truncated and rebuilt rather than being a second source of truth.

    Article work only. Summarize work has a cluster subject and derives from
    ``Cluster.distinct_source_count`` and ``Summary.input_fingerprint`` instead.
    """
    rows = session.execute(
        sa.select(
            CanonicalRecord.article_id,
            CanonicalRecord.pipeline_state,
            CanonicalRecord.terminal_reason,
        ).where(
            CanonicalRecord.terminal_reason.is_(None),
            CanonicalRecord.pipeline_state.in_(_OWING_STATES),
        )
    ).all()
    owed = {(article_id, owed_stage(state, terminal)) for article_id, state, terminal in rows}
    return {(article_id, stage) for article_id, stage in owed if stage is not None}


def open_article_work(session: Session) -> set[tuple[int, Stage]]:
    """``(article_id, stage)`` pairs the queue currently holds, excluding dead-lettered rows."""
    rows = session.execute(
        sa.select(PipelineWork.article_id, PipelineWork.stage).where(
            PipelineWork.article_id.is_not(None),
            PipelineWork.dead_lettered_at.is_(None),
        )
    ).all()
    return {(article_id, stage) for article_id, stage in rows if article_id is not None}


def missing_article_work(session: Session) -> set[tuple[int, Stage]]:
    """What the records say is owed but the queue does not hold.

    A healthy system returns the empty set; a non-zero result is a defect in a stage handler,
    not a repair to absorb quietly (RFC §6.3).
    """
    return expected_article_work(session) - open_article_work(session)


class StaleWork(Exception):
    """The work row is no longer ours to complete — another worker discharged it first.

    Raised rather than ignored. A caller that treats a vanished row as success has completed a
    stage twice: two sets of stage output, two successors attempted, and for a model-backed
    stage two lots of inference paid for.
    """


#: Stages whose subject is an article. ``SUMMARIZE`` acts on a cluster and is not in the chain.
_ARTICLE_STAGES = frozenset(stage for stage, _ in ARTICLE_CHAIN)


def _check_subject(work: PipelineWork) -> None:
    """Refuse a work row whose subject is the wrong kind for its stage.

    ``exactly_one_subject`` enforces that a row has one subject, not that it has the *right*
    one. A row with ``stage='acquire'`` and a cluster subject otherwise advances silently to a
    successor row that is also mis-subjected — and both ``expected_article_work`` and
    ``open_article_work`` skip cluster rows, so the reconciler can never see it. ``terminate``
    already refuses this; the two are symmetric on purpose.
    """
    if work.stage in _ARTICLE_STAGES:
        if work.article_id is None:
            raise ValueError(
                f"stage {work.stage} acts on an article; work {work.work_id} has a cluster subject"
            )
    elif work.article_id is not None:
        raise ValueError(
            f"stage {work.stage} acts on a cluster; work {work.work_id} has an article subject"
        )


def _discharge(session: Session, work: PipelineWork) -> None:
    """Delete the work row, and refuse to continue if it was not ours to delete.

    ⚠️ ``session.delete()`` matching zero rows emits a ``SAWarning`` and carries on. That is
    the whole failure: a worker whose lease expired, whose row was claimed and completed by
    somebody else, would advance the article a second time. At ``acquire`` a unique index on
    the successor happens to stop it; at the end of the chain there is no successor and so
    nothing catches it at all.

    Issued as a statement rather than through the unit of work for a second reason: it emits
    the DELETE now. SQLAlchemy orders INSERTs ahead of DELETEs within one flush, and
    ``uq_pipeline_work_open_article`` permits one open row per article, so a queued delete plus
    a queued successor insert raises ``UniqueViolation`` on every advance.
    """
    discharged = session.scalar(
        sa.delete(PipelineWork)
        .where(PipelineWork.work_id == work.work_id)
        .returning(PipelineWork.work_id)
    )
    if discharged is None:
        raise StaleWork(
            f"work {work.work_id} was already discharged; another worker completed this stage"
        )
    session.expunge(work)


def advance(session: Session, work: PipelineWork) -> None:
    """Complete a stage: move the subject's state, discharge this row, project, enqueue the next.

    Every write that ends a stage, in one place — which is the point of it existing (RFC
    §6.2). A handler that writes its output and forgets the enqueue leaves an article
    stopped with no error, no attempt count and no dead-letter row, and **no alarm can fire,
    because every alarm hangs off the work row that is now gone.** With one helper, exactly
    one place in the system can make that mistake, and one test covers it. The read-model
    projection (§6.3) is here for the same reason and fails the same way: an article that is
    finished and unreadable is invisible to the reconciler, which asks pipeline_state.

    Does not commit. The caller has already written the stage's own output onto this session,
    and a single commit is what makes all of it one transaction — a state that moved without
    its successor being enqueued is precisely the failure above.

    The order comes from ``libs/contract``: which state this stage leaves the subject in, and
    which stage follows. Nothing here restates it, so adding a stage is still a one-line edit
    to ``ARTICLE_CHAIN``.

    Raises ``StaleWork`` if the row has already been discharged — see ``_discharge``. The
    caller must not treat that as a completion.
    """
    _check_subject(work)
    new_state = STATE_AFTER_STAGE[work.stage]
    successor = STAGE_SUCCESSOR[work.stage]

    if work.article_id is not None and new_state is not None:
        article = session.get(CanonicalRecord, work.article_id)
        if article is None:
            raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")
        article.pipeline_state = new_state

    article_id, cluster_id = work.article_id, work.cluster_id
    _discharge(session, work)

    # The read model is refreshed here, inside the same transaction, for the reason this
    # helper exists at all: a projection written after the commit leaves a window in which
    # the subject is finished and unreadable, and nothing can find it afterwards — §6.3's
    # reconciler derives owed work from pipeline_state, which already says it is done. After
    # the discharge, not before, so a worker whose lease expired projects nothing: the
    # projection is idempotent, but doing the work and then finding out it was not ours is
    # the shape this helper exists to remove.
    #
    # RFC §6.3's two triggers, and neither is "the pipeline finished with this cluster":
    # an article becoming readable, and a cluster's summary changing.
    if article_id is not None and new_state is PROJECTABLE_STATE:
        project_article_cluster(session, article_id)
    elif cluster_id is not None:
        project_cluster(session, cluster_id)

    if successor is not None:
        session.add(PipelineWork(article_id=article_id, cluster_id=cluster_id, stage=successor))
        session.flush()


def terminate(session: Session, work: PipelineWork, reason: TerminalReason) -> None:
    """The article stops here for good: record why, discharge the work row, enqueue nothing.

    Not a failure path. A dropped article is a decision, and the row survives it deliberately:
    ``expected_article_work`` skips records with a ``terminal_reason``, so nothing re-enqueues
    it, and discovery's ``UNIQUE(source_id, guid)`` recognises it on the next poll and inserts
    nothing. Delete the record instead and every poll rediscovers, re-decides and re-drops the
    same article forever, hitting the publisher each time.

    ``pipeline_state`` deliberately does not move — the stage did not complete, it concluded
    the article should not continue, and those are different claims.

    Does not commit.
    """
    if work.article_id is None:
        raise ValueError(
            f"work {work.work_id} has a cluster subject; terminal_reason lives on an article"
        )
    article = session.get(CanonicalRecord, work.article_id)
    if article is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")
    article.terminal_reason = reason
    _discharge(session, work)
