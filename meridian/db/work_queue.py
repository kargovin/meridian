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

from meridian.db.models import AlternateCopy, CanonicalRecord, ClusterMember, PipelineWork
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
            # ⚠️ populate_existing, or a row the session already holds comes back with its
            # pre-claim attributes: RETURNING does not overwrite loaded state on an identity-map
            # instance by default, so ``attempts`` reads one too low and ``claimed_by`` reads the
            # previous holder. Invisible to a caller that meets the row here for the first time.
            execution_options={"synchronize_session": False, "populate_existing": True},
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


def collapse(session: Session, work: PipelineWork, *, into: int) -> None:
    """This article is a second copy of ``into``: keep the provenance, drop the record.

    The third way a work row ends, and neither of the other two fits. ``advance()`` would send
    the duplicate on to be classified, embedded and clustered as a story of its own, which is
    the double-counting FR-I5 exists to prevent. ``terminate()`` keeps the record, and a record
    that stays is still a cluster candidate — and it records nothing about the publisher whose
    copy this was. A duplicate is neither finished nor rejected; it is a record that turned out
    to be a second copy of one we already hold.

    Collapse-not-drop is a foreign key, not a delete (RFC §5.2): the ``AlternateCopy`` keeps the
    publisher, URL, guid, feed and publication date, so cross-source coverage is never
    undercounted (US-K2). The count that reads them is ``1 + count(DISTINCT source_id)``.

    ⚠️ The order below is not arbitrary. ``pipeline_work`` cascades from ``canonical_record``,
    so deleting the record first takes this work row with it and the discharge — a conditional
    DELETE that raises on zero rows — then reports that another worker completed a row we
    destroyed ourselves. Discharge first, while the row is still ours to check. For the same
    reason the duplicate's fields are read into locals before the delete: afterwards, touching
    the instance re-queries a row that is gone.

    Does not commit. The note, the deletion, the discharge and the projection are one
    transaction, or a duplicate can be dropped with its provenance unwritten.
    """
    if work.article_id is None:
        raise ValueError(
            f"work {work.work_id} has a cluster subject; only an article can be collapsed"
        )
    if work.article_id == into:
        raise ValueError(
            f"article {into} cannot collapse into itself; the match query returned its own row"
        )

    duplicate = session.get(CanonicalRecord, work.article_id)
    if duplicate is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")
    # Read before the delete: a rollback or a delete expires the instance, and every attribute
    # access after that re-queries a row that no longer exists.
    copy_of = dict(
        source_id=duplicate.source_id,
        feed_id=duplicate.feed_id,
        url=duplicate.url_canonical,
        guid=duplicate.guid,
        published_at=duplicate.published_at,
    )

    # ⚠️ Our lock, taken before anything is written. The AlternateCopy's foreign key would take
    # only FOR KEY SHARE on the representative, and a foreign key's lock protects referential
    # integrity, not this function's invariant — that what we are attaching to is still a
    # representative at the moment we attach to it.
    locked = session.scalar(
        sa.select(CanonicalRecord.article_id)
        .where(CanonicalRecord.article_id == into)
        .with_for_update()
    )
    if locked is None:
        raise ValueError(
            f"article {into} is gone; nothing to collapse article {work.article_id} into"
        )

    # ⚠️ ``alternate_copy`` cascades from ``canonical_record`` too, so collapsing a record that
    # is already somebody's representative would delete its notes with it — losing exactly the
    # provenance this function exists to keep, with nothing raised. Unreachable today, because
    # only a record still at this stage is collapsed and a representative has already passed
    # it; a guard makes that true by construction rather than by argument.
    held = session.scalar(
        sa.select(AlternateCopy.alternate_copy_id)
        .where(AlternateCopy.article_id == work.article_id)
        .limit(1)
    )
    if held is not None:
        raise ValueError(
            f"article {work.article_id} already holds collapsed copies; collapsing it would "
            "delete their provenance by cascade"
        )

    article_id = work.article_id
    _discharge(session, work)

    session.add(AlternateCopy(article_id=into, **copy_of))
    session.flush()
    session.execute(sa.delete(CanonicalRecord).where(CanonicalRecord.article_id == article_id))

    # RFC §6.3: the duplicate never enters the article chain and the representative has no
    # stage left to complete, so neither projection trigger fires and the coverage list would
    # silently omit the publisher collapse-not-drop just preserved. ``project_article_cluster``
    # refuses an unclustered article by design; here that is the ordinary case, so the
    # membership is looked up and only a real cluster is re-projected.
    cluster_id = session.scalar(
        sa.select(ClusterMember.cluster_id).where(ClusterMember.article_id == into)
    )
    if cluster_id is not None:
        project_cluster(session, cluster_id)


def heartbeat(
    session: Session,
    work_ids: Sequence[int],
    *,
    worker: str,
    now: dt.datetime | None = None,
) -> set[int]:
    """Restamp the rows this worker still holds, and say which those are. Commits.

    A claim's stamp is a promise to finish within the lease; a stage that waits on the network
    cannot keep it for a whole batch (50 rows paced at 12 s is 600 s under a 300 s lease), so
    the promise is renewed after each row and the lease bounds one row's work rather than the
    batch's. A worker that dies stops renewing, and its rows become reclaimable one lease after
    its last heartbeat — the same recovery as before, measured from a different moment.

    ⚠️ Never routes through ``claim()``'s update: that increments ``attempts``, and a heartbeat
    is not an attempt. Fifty rows in a batch would otherwise count as fifty attempts on every
    row and exhaust all of their retries at once.

    ``WHERE claimed_by = worker`` is what makes the return value an ownership check: a row
    another worker reclaimed keeps that worker's stamp and is absent from the result, so the
    caller drops it before doing work that ``advance()`` would then refuse as ``StaleWork``.
    """
    if not work_ids:
        return set()
    now = now or dt.datetime.now(dt.UTC)
    kept = session.scalars(
        sa.update(PipelineWork)
        .where(PipelineWork.work_id.in_(list(work_ids)), PipelineWork.claimed_by == worker)
        .values(claimed_at=now)
        .returning(PipelineWork.work_id),
        execution_options={"synchronize_session": False},
    ).all()
    session.commit()
    return set(kept)


#: Backoff after a transient failure: 1, 2, 4, 8, 16 minutes, then the cap. Constants rather
#: than knobs for now; with the lease they are one retry policy and belong in one place.
RETRY_BASE = dt.timedelta(minutes=1)
RETRY_CAP = dt.timedelta(hours=1)
#: The attempt on which a stage stops retrying a transient failure. What it does instead is
#: the stage's own call: acquire gives up the fetch and moves the article on without a body;
#: ``dead_letter`` is for a stage whose output the article cannot continue without.
MAX_ATTEMPTS = 5


def backoff_after(attempts: int) -> dt.timedelta:
    """How long a row waits before its next attempt, given how many it has had."""
    wait: dt.timedelta = RETRY_BASE * (2 ** max(attempts - 1, 0))
    return min(wait, RETRY_CAP)


def release(
    session: Session,
    work: PipelineWork,
    *,
    retry_at: dt.datetime,
    error: str | None,
    strike: bool = True,
) -> None:
    """Give the row back unfinished: clear the claim, set when it is next due, record why.

    For the failure a later attempt may not see — a publisher's 503, a timeout, a pacing wait
    longer than the lease. A strike keeps the ``attempts`` the claim counted, so repeated
    releases walk up the backoff towards ``MAX_ATTEMPTS``; ``strike=False`` gives that count
    back, for a release that is not the row's fault — a pacing wait is one — and must not walk
    it anywhere however often it recurs.

    ⚠️ A conditional UPDATE on our own claim, never a write through the instance. The instance
    is in the identity map, so ``session.get`` would answer from it without a query and could
    not notice that another worker reclaimed or completed the row after our lease expired: a
    write through it would then clear *their* claim, or raise ``StaleDataError`` at flush for a
    row that is gone. No match means the row was never ours to give back.

    ⚠️ Refuses an instance with no claim on it. ``claimed_by == None`` compiles to ``IS NULL``,
    which matches every *unclaimed* row by id — the opposite of ownership — and the caller
    that loaded a row rather than claiming it would hand it back with ``attempts`` walked
    below zero. A row must come from ``claim()``.

    Does not commit. The caller's rollback has already discarded the stage's partial output;
    this is written on the clean session and committed by the caller.
    """
    _require_claim(work)
    released = session.scalar(
        sa.update(PipelineWork)
        .where(PipelineWork.work_id == work.work_id, PipelineWork.claimed_by == work.claimed_by)
        .values(
            claimed_at=None,
            claimed_by=None,
            next_attempt_at=retry_at,
            last_error=error[:2000] if error else None,
            attempts=PipelineWork.attempts if strike else PipelineWork.attempts - 1,
        )
        .returning(PipelineWork.work_id)
    )
    if released is None:
        raise StaleWork(f"work {work.work_id} is no longer ours; another worker reclaimed it")
    session.expunge(work)


def _require_claim(work: PipelineWork) -> None:
    if work.claimed_by is None:
        raise ValueError(
            f"work {work.work_id} carries no claim; only a row from claim() can be given back"
        )


def dead_letter(session: Session, work: PipelineWork, *, error: str | None) -> None:
    """Stop retrying: keep the row as the trace, and mark the article terminal.

    ⚠️ Both writes, or the reconciler resurrects its own dead letters. ``open_article_work``
    excludes dead-lettered rows and ``expected_article_work`` only excludes articles carrying a
    ``terminal_reason`` — so a dead-lettered row alone reads as work owed forever, and the
    re-enqueue succeeds because the open-row index also ignores dead-lettered rows. This spans
    two tables and no CHECK can state it.

    The row is written by a conditional UPDATE on our own claim, and refuses an unclaimed
    instance, for the reasons ``release`` gives. Does not commit.
    """
    _require_claim(work)
    if work.article_id is None:
        raise ValueError(
            f"work {work.work_id} has a cluster subject; terminal_reason lives on an article"
        )
    article = session.get(CanonicalRecord, work.article_id)
    if article is None:
        raise ValueError(f"work {work.work_id} names article {work.article_id}, which is gone")
    dead = session.scalar(
        sa.update(PipelineWork)
        .where(PipelineWork.work_id == work.work_id, PipelineWork.claimed_by == work.claimed_by)
        .values(
            dead_lettered_at=dt.datetime.now(dt.UTC),
            claimed_at=None,
            claimed_by=None,
            last_error=error[:2000] if error else None,
        )
        .returning(PipelineWork.work_id)
    )
    if dead is None:
        raise StaleWork(f"work {work.work_id} is no longer ours; another worker reclaimed it")
    session.expunge(work)
    article.terminal_reason = TerminalReason.FAILED
