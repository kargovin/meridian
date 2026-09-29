"""The acquire stage and the transition helpers it uses (MER-17's acceptance criteria).

Against a real PostgreSQL: the partial unique index on ``pipeline_work`` and ``SKIP LOCKED``
are both load-bearing here and neither exists on SQLite.
"""

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db import reconcile, work_queue
from meridian.db.models import CanonicalRecord, PipelineWork
from meridian.db.session import session_factory
from meridian.ingest.acquire import AcquireReport, run_batch
from meridian.ingest.acquire import handle as _handle
from meridian.ingest.fetch import FetchResult
from meridian.ingest.network import Network
from meridian.ingest.pacing import Pacer
from meridian.ingest.robots import RobotsCache
from tests.factories import make_article, make_cluster, make_member, make_source, make_work

pytestmark = pytest.mark.postgres

LEASE = dt.timedelta(minutes=5)


def _no_network(url: str, *, user_agent: str, headers: object) -> FetchResult:
    """Every article in this file has no feed, so no rung of the ladder may reach the network."""
    raise AssertionError(f"the network was reached for {url}")


_pacer = Pacer(sleep=lambda _: None)
NETWORK = Network(fetcher=_no_network, robots=RobotsCache(_no_network, _pacer), pacer=_pacer)


def handle(session: Session, work: PipelineWork) -> bool:
    """The MER-17 shape: True when the article continued, False when FR-I7 dropped it."""
    report = _handle(session, work, network=NETWORK, lease=LEASE)
    assert report.acquired + report.dropped == 1, report
    return report.acquired == 1


def _queued(session: Session, article_id: int) -> list[PipelineWork]:
    return list(
        session.scalars(sa.select(PipelineWork).where(PipelineWork.article_id == article_id)).all()
    )


# --------------------------------------------------------------------------- advance


def test_advance_moves_the_state_and_enqueues_the_successor(app_session: Session) -> None:
    """The four writes that end every stage.

    ⚠️ Also the falsifier for an ordering trap: SQLAlchemy emits INSERTs before DELETEs within
    one flush, and ``uq_pipeline_work_open_article`` permits one open row per article — so the
    natural spelling of "delete this row, add the next" raises UniqueViolation on *every*
    advance. Verified by removing the intermediate flush.
    """
    source = make_source(app_session)
    article = make_article(app_session, source)
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    work_queue.advance(app_session, work)
    app_session.commit()

    assert article.pipeline_state is PipelineState.ACQUIRED
    remaining = _queued(app_session, article.article_id)
    assert [row.stage for row in remaining] == [Stage.DEDUP]


def test_advance_at_the_end_of_the_chain_enqueues_nothing(app_session: Session) -> None:
    """``cluster`` is last. The article is done, not owed a stage that does not exist."""
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.CLASSIFIED)
    # Membership is not decoration: completing ``cluster`` also projects the article into
    # the read model, and one that reached the end of the chain belonging to no cluster is
    # refused rather than left finished and unreadable.
    make_member(app_session, make_cluster(app_session), article)
    work = make_work(app_session, stage=Stage.CLUSTER, article=article)

    work_queue.advance(app_session, work)
    app_session.commit()

    assert article.pipeline_state is PipelineState.CLUSTERED
    assert _queued(app_session, article.article_id) == []


def test_a_stage_completion_is_atomic(app_session: Session) -> None:
    """AC3. The state move and the enqueue are one transaction or neither happens.

    ⚠️ This is the failure the helper exists to prevent, and it is invisible when it happens:
    an article advanced with nothing enqueued has no error, no attempt count and no
    dead-letter row, and no alarm can fire because every alarm hangs off the work row that is
    now gone. Here the enqueue is made to fail; the state must not have moved.
    """
    source = make_source(app_session)
    article = make_article(app_session, source)
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    original = article.pipeline_state

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("enqueue failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(app_session, "add", explode)
        with pytest.raises(RuntimeError):
            work_queue.advance(app_session, work)
    app_session.rollback()

    app_session.expire_all()
    survivor = app_session.get(CanonicalRecord, article.article_id)
    assert survivor is not None
    assert survivor.pipeline_state is original
    assert [row.stage for row in _queued(app_session, article.article_id)] == [Stage.ACQUIRE]


def test_terminate_refuses_cluster_work(app_session: Session) -> None:
    """``terminal_reason`` lives on an article; summarize work has a cluster subject."""
    cluster = make_cluster(app_session)
    work = make_work(app_session, stage=Stage.SUMMARIZE, cluster=cluster)

    with pytest.raises(ValueError, match="cluster subject"):
        work_queue.terminate(app_session, work, TerminalReason.DROPPED_LANGUAGE)


# --------------------------------------------------------------------------- the stage


def test_an_english_article_is_normalized_and_handed_on(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(
        app_session,
        source,
        title="Storm Bertha closes ports across the south coast",
        lede="<p>Ferry operators have <b>suspended</b> sailings.</p>",
    )
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    assert handle(app_session, work) is True

    assert article.lede == "Ferry operators have suspended sailings."
    assert article.language == "en"
    assert article.pipeline_state is PipelineState.ACQUIRED
    assert article.terminal_reason is None
    assert [row.stage for row in _queued(app_session, article.article_id)] == [Stage.DEDUP]


def test_the_hashes_are_left_null_until_there_is_a_body(app_session: Session) -> None:
    """Both are specified over the article body (2.1.2 §3.2), and no feed on the roster ships
    one. Computed over a headline instead, SHA-256 declares two different articles identical —
    which collapses a real article and inflates the distinct-source count FR-S6 gates on.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, title="Storm Bertha closes ports")
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    handle(app_session, work)

    assert article.content_hash is None
    assert article.simhash is None


def test_a_non_english_article_stops_and_keeps_its_row(app_session: Session) -> None:
    """AC1, first half."""
    source = make_source(app_session)
    article = make_article(
        app_session,
        source,
        title="Por que Cuba no produce suficiente comida para alimentar a su poblacion",
    )
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    assert handle(app_session, work) is False

    assert article.terminal_reason is TerminalReason.DROPPED_LANGUAGE
    assert article.language == "es"
    # The stage concluded the article should not continue; it did not complete the stage.
    assert article.pipeline_state is PipelineState.DISCOVERED
    assert _queued(app_session, article.article_id) == []


def test_a_dropped_article_is_never_owed_work_again(app_session: Session) -> None:
    """AC1, and the half that makes the surviving row worth keeping.

    ⚠️ Delete the record instead and every poll rediscovers, re-detects and re-drops the same
    article forever, hitting the publisher each time and reporting nothing. The reconciler
    derives what is owed from ``pipeline_state`` and ``terminal_reason``, so this is what stops
    it putting the work back.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, title="Mercados caen hoy en Madrid y Barcelona")
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    handle(app_session, work)

    assert reconcile.survey(app_session) == []
    assert reconcile.run(app_session).clean


def test_a_second_batch_does_not_re_examine_a_dropped_article(app_session: Session) -> None:
    """The queue is empty afterwards, so the batch has nothing to claim."""
    source = make_source(app_session)
    article = make_article(app_session, source, title="Mercados caen hoy en Madrid y Barcelona")
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    first = run_batch(app_session, network=NETWORK, lease=LEASE)
    second = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert (first.claimed, first.dropped) == (1, 1)
    assert second.claimed == 0


# --------------------------------------------------------------------------- the batch


def test_one_bad_article_does_not_stop_the_batch(app_session: Session) -> None:
    """⚠️ Same shape as discovery's per-feed guard. Without it a single record that provokes a
    bug freezes the stage for every other article, and the symptom is a queue that stops
    draining with nothing in the log to say which row did it.
    """
    source = make_source(app_session)
    good = make_article(app_session, source, guid="ok", title="Storm Bertha closes the ports")
    bad = make_article(app_session, source, guid="bad", title="Second headline here")
    make_work(app_session, stage=Stage.ACQUIRE, article=good)
    make_work(app_session, stage=Stage.ACQUIRE, article=bad)
    app_session.commit()

    real = work_queue.advance

    def explode_for_bad(session: Session, work: PipelineWork) -> None:
        if work.article_id == bad.article_id:
            raise RuntimeError("boom")
        real(session, work)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", explode_for_bad)
        report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert (report.claimed, report.acquired, report.failed) == (2, 1, 1)
    app_session.expire_all()
    survivor = app_session.get(CanonicalRecord, good.article_id)
    assert survivor is not None
    assert survivor.pipeline_state is PipelineState.ACQUIRED


def test_a_failure_records_what_it_failed_on(app_session: Session) -> None:
    """⚠️ The rollback that contains the failure also discards anything written inside it, so
    a message written in the handler's transaction goes with it — leaving the one failure
    class the guard exists to survive as the one that says nothing about itself. ``attempts``
    reports only how often.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, title="Storm Bertha closes the ports")
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("a very specific failure")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", explode)
        run_batch(app_session, network=NETWORK, lease=LEASE)

    app_session.expire_all()
    (row,) = _queued(app_session, article.article_id)
    assert row.last_error is not None
    assert "a very specific failure" in row.last_error
    assert row.attempts == 1


def _always_raises(session: Session, *, attempts: int) -> int:
    """An acquire row whose handler raises something the ladder did not classify. Returns its
    article id."""
    source = make_source(session)
    article = make_article(session, source, guid="broken", title="Storm Bertha closes the ports")
    make_work(session, stage=Stage.ACQUIRE, article=article, attempts=attempts)
    session.commit()
    return article.article_id


def _explode(*_: object, **__: object) -> None:
    raise RuntimeError("unexpected")


def test_an_unexpected_error_gives_the_row_back_with_backoff(app_session: Session) -> None:
    """⚠️ Not left claimed: a claimed row is taken again one lease later, with no backoff and no
    limit, for as long as the error recurs."""
    article_id = _always_raises(app_session, attempts=0)
    before = dt.datetime.now(dt.UTC)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", _explode)
        report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert report == AcquireReport(claimed=1, failed=1)
    app_session.expire_all()
    (row,) = _queued(app_session, article_id)
    assert row.claimed_by is None
    assert row.dead_lettered_at is None
    assert row.next_attempt_at >= before + work_queue.backoff_after(1)
    assert row.last_error == "RuntimeError: unexpected"


def test_an_unexpected_error_on_every_attempt_is_dead_lettered(app_session: Session) -> None:
    """On the attempt MAX_ATTEMPTS names the row is given up and the article marked failed, so
    the reconciler neither re-enqueues it nor reads it as healthy. Not the fetch give-up's
    headline-only continuation: the error may be in writing the body."""
    article_id = _always_raises(app_session, attempts=work_queue.MAX_ATTEMPTS - 1)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", _explode)
        report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert report == AcquireReport(claimed=1, failed=1, dead_lettered=1)
    app_session.expire_all()
    (row,) = _queued(app_session, article_id)
    assert row.dead_lettered_at is not None
    assert row.last_error == "RuntimeError: unexpected"
    article = app_session.get(CanonicalRecord, article_id)
    assert article is not None
    assert article.terminal_reason is TerminalReason.FAILED
    assert article.pipeline_state is PipelineState.DISCOVERED


def test_an_unexpected_error_one_attempt_short_is_not_dead_lettered(app_session: Session) -> None:
    article_id = _always_raises(app_session, attempts=work_queue.MAX_ATTEMPTS - 2)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", _explode)
        report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert report == AcquireReport(claimed=1, failed=1)
    app_session.expire_all()
    (row,) = _queued(app_session, article_id)
    assert row.dead_lettered_at is None
    article = app_session.get(CanonicalRecord, article_id)
    assert article is not None and article.terminal_reason is None


def test_an_unexpected_error_does_not_touch_a_row_another_worker_has_reclaimed(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """⚠️ Our lease runs out mid-article and another worker claims the row, then our handler
    fails. Reloaded after the rollback, the row names the other worker — an ownership check
    against the reloaded row would pass, and we would dead-letter their live claim."""
    article_id = _always_raises(app_session, attempts=work_queue.MAX_ATTEMPTS - 2)

    def reclaimed_then_raise(session: Session, work: PipelineWork) -> None:
        with session_factory(app_migrated)() as other:
            taken = work_queue.claim(
                other, stage=Stage.ACQUIRE, worker="other", lease=dt.timedelta(0)
            )
            assert [row.work_id for row in taken] == [work.work_id]
        raise RuntimeError("commit failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.work_queue.advance", reclaimed_then_raise)
        report = run_batch(app_session, network=NETWORK, lease=LEASE, worker="ours")

    assert report == AcquireReport(claimed=1, failed=1)
    app_session.expire_all()
    (row,) = _queued(app_session, article_id)
    assert row.claimed_by == "other"
    assert row.attempts == work_queue.MAX_ATTEMPTS
    assert row.dead_lettered_at is None
    article = app_session.get(CanonicalRecord, article_id)
    assert article is not None and article.terminal_reason is None


def test_the_batch_respects_its_limit(app_session: Session) -> None:
    source = make_source(app_session)
    for n in range(5):
        article = make_article(app_session, source, guid=f"g{n}", title=f"Headline number {n}")
        make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    report = run_batch(app_session, network=NETWORK, lease=LEASE, limit=2)

    assert report.claimed == 2


def test_the_batch_claims_only_its_own_stage(app_session: Session) -> None:
    """Every stage shares one table; a handler that claimed another's work would run the
    wrong code against the wrong subject.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.ACQUIRED)
    make_work(app_session, stage=Stage.DEDUP, article=article)
    app_session.commit()

    assert run_batch(app_session, network=NETWORK, lease=LEASE).claimed == 0


# --------------------------------------------------------------------------- the lost lease


def _steal(app_session: Session, work: PipelineWork) -> None:
    """What another worker does to a row whose lease we let expire: completes and deletes it.

    Committed through the same session, because what matters to the code under test is that
    the row is gone from the database, not which connection removed it.
    """
    app_session.execute(sa.delete(PipelineWork).where(PipelineWork.work_id == work.work_id))
    app_session.commit()


def test_advance_refuses_a_row_another_worker_already_discharged(app_session: Session) -> None:
    """⚠️ ``session.delete()`` matching zero rows WARNS and carries on — it does not raise.

    So a worker whose lease expired, whose row was claimed and finished by somebody else,
    would complete the stage a second time. At ``acquire`` the successor's unique index happens
    to stop it; that is luck, not a guard, and the next test shows where the luck runs out.
    """
    source = make_source(app_session)
    article = make_article(app_session, source)
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    _steal(app_session, work)

    with pytest.raises(work_queue.StaleWork):
        work_queue.advance(app_session, work)


def test_advance_refuses_a_stolen_row_at_the_end_of_the_chain(app_session: Session) -> None:
    """⚠️ The case with nothing else to catch it.

    ``cluster`` is the last article stage, so ``STAGE_SUCCESSOR`` is None and there is no
    insert to collide with a unique index. Before the discharge check, this advanced silently:
    two workers, two completions, no error anywhere. That is the tail of the chain today and
    the two-minute model-backed stage tomorrow.
    """
    source = make_source(app_session)
    article = make_article(app_session, source, state=PipelineState.CLASSIFIED)
    work = make_work(app_session, stage=Stage.CLUSTER, article=article)
    app_session.commit()

    _steal(app_session, work)

    with pytest.raises(work_queue.StaleWork):
        work_queue.advance(app_session, work)


def test_terminate_refuses_a_stolen_row(app_session: Session) -> None:
    """Same guard on the other transition — it writes ``terminal_reason`` onto a live article."""
    source = make_source(app_session)
    article = make_article(app_session, source, title="Mercados caen hoy en Madrid y Barcelona")
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    _steal(app_session, work)

    with pytest.raises(work_queue.StaleWork):
        work_queue.terminate(app_session, work, TerminalReason.DROPPED_LANGUAGE)


def test_a_stolen_row_does_not_take_down_the_rest_of_the_batch(app_session: Session) -> None:
    """⚠️ B2 and B3 together, in the arrangement that actually happens.

    Rolling k3s deploys kill pods on every push, so a worker resuming against rows another
    process already finished is the normal case rather than an exotic one.

    Before the fix this failed twice over. ``advance`` completed the stolen row silently, and
    where it did raise, the guard that was supposed to contain the failure raised
    ``ObjectDeletedError`` *while logging it* — ``work.article_id`` on a rolled-back, deleted
    instance re-queries a row that is gone. That escaped ``run_batch`` entirely, so the two
    healthy articles were never processed and nothing was recorded anywhere.

    The theft happens inside the batch, between the claim and the handler, because that is the
    only window in which it is interesting.
    """
    source = make_source(app_session)
    stolen = make_article(app_session, source, guid="a", title="First headline about the storm")
    good_a = make_article(app_session, source, guid="b", title="Second headline on the floods")
    good_b = make_article(app_session, source, guid="c", title="Third headline about the winds")
    for article in (stolen, good_a, good_b):
        make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    def steal_then_handle(session: Session, work: PipelineWork, **kw: Any) -> AcquireReport:
        if work.article_id == stolen.article_id:
            session.execute(sa.delete(PipelineWork).where(PipelineWork.work_id == work.work_id))
            session.commit()
        return _handle(session, work, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.handle", steal_then_handle)
        report = run_batch(app_session, network=NETWORK, lease=LEASE, limit=10)

    assert (report.claimed, report.acquired, report.stale, report.failed) == (3, 2, 1, 0)

    app_session.expire_all()
    for article in (good_a, good_b):
        survivor = app_session.get(CanonicalRecord, article.article_id)
        assert survivor is not None
        assert survivor.pipeline_state is PipelineState.ACQUIRED
    # The stolen article keeps whatever the other worker left it at, and owes nothing extra —
    # and nothing our handler wrote onto it before the discharge refused survives: the StaleWork
    # branch's rollback discards it, or the next article's commit would carry it in.
    assert _queued(app_session, stolen.article_id) == []
    untouched = app_session.execute(
        sa.select(CanonicalRecord.pipeline_state, CanonicalRecord.language).where(
            CanonicalRecord.article_id == stolen.article_id
        )
    ).one()
    assert tuple(untouched) == (PipelineState.DISCOVERED, None)


def test_advance_refuses_a_stage_and_subject_that_do_not_match(app_session: Session) -> None:
    """⚠️ ``exactly_one_subject`` enforces that a row has one subject, not the right one.

    An article stage carrying a cluster subject otherwise advances into a successor row that is
    also mis-subjected — and both halves of the reconciler skip cluster rows, so nothing can
    ever see it. ``terminate`` already refused this; the two are symmetric now.
    """
    from tests.factories import make_cluster

    cluster = make_cluster(app_session)
    work = make_work(app_session, stage=Stage.ACQUIRE, cluster=cluster)

    with pytest.raises(ValueError, match="acts on an article"):
        work_queue.advance(app_session, work)


def test_an_article_deleted_mid_batch_does_not_take_down_the_rest(app_session: Session) -> None:
    """⚠️ The case that reaches the *generic* failure branch with a row that no longer exists.

    A takedown removing an article is a real operation (RFC §10), and ``pipeline_work`` cascades
    on ``article_id``, so the work row goes with it. A deletion alone now reaches the StaleWork
    branch (the handler and ``_lock_article`` both report a gone article as stale); the generic
    branch meets a gone row only when some other error follows the deletion, which is why this
    test raises one itself. The handler then raises — and before the
    fix the guard raised *while logging that failure*: ``work.article_id`` on a rolled-back,
    deleted instance re-queries a row that is gone and throws ``ObjectDeletedError``, which
    escaped ``run_batch`` and stranded every remaining article in the batch.

    The stolen-row test above does not cover this: it raises ``StaleWork``, which is handled by
    a branch that never touches the instance. This one has to be its own test.
    """
    source = make_source(app_session)
    doomed = make_article(app_session, source, guid="a", title="First headline about the storm")
    good_a = make_article(app_session, source, guid="b", title="Second headline on the floods")
    good_b = make_article(app_session, source, guid="c", title="Third headline about the winds")
    for article in (doomed, good_a, good_b):
        make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()

    def delete_then_handle(session: Session, work: PipelineWork, **kw: Any) -> AcquireReport:
        if work.article_id == doomed.article_id:
            session.execute(
                sa.delete(CanonicalRecord).where(CanonicalRecord.article_id == doomed.article_id)
            )
            session.commit()
            session.expire_all()
            # An unexpected error, not the handler's own "gone" check — which reports StaleWork
            # and never reaches the generic branch this test is for.
            raise RuntimeError("the takedown raced the handler")
        return _handle(session, work, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("meridian.ingest.acquire.handle", delete_then_handle)
        report = run_batch(app_session, network=NETWORK, lease=LEASE, limit=10)

    assert (report.claimed, report.acquired, report.failed) == (3, 2, 1)

    app_session.expire_all()
    for article in (good_a, good_b):
        survivor = app_session.get(CanonicalRecord, article.article_id)
        assert survivor is not None
        assert survivor.pipeline_state is PipelineState.ACQUIRED


def test_an_article_gone_before_the_handler_loads_it_is_stale_not_failed(
    app_session: Session, app_migrated: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removed between the claim and the handler — a takedown, or a collapse by the worker that
    reclaimed the row — and the work row cascaded with it. Somebody else ended this work."""
    source = make_source(app_session)
    article = make_article(app_session, source, title="Storm Bertha closes the ports")
    make_work(app_session, stage=Stage.ACQUIRE, article=article)
    app_session.commit()
    article_id = article.article_id
    real_claim = work_queue.claim

    def claim_then_delete(*args: Any, **kw: Any) -> Any:
        claimed = real_claim(*args, **kw)
        with app_migrated.begin() as other:
            other.execute(
                sa.text("DELETE FROM canonical_record WHERE article_id = :id"), {"id": article_id}
            )
        return claimed

    # The batch's session has not loaded this article, as in a running process; a held
    # instance would answer ``session.get`` from the identity map and hide the deletion.
    app_session.expunge_all()
    monkeypatch.setattr("meridian.ingest.acquire.work_queue.claim", claim_then_delete)
    report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert report == AcquireReport(claimed=1, stale=1)


def test_a_row_with_a_cluster_subject_fails_rather_than_reading_as_stale(
    app_session: Session,
) -> None:
    """A defect in whatever enqueued it: acquire acts on an article. With no article to load it
    must not read as one somebody else removed — stale is silent, and the row would be re-taken
    every lease with nothing written about why."""
    make_work(app_session, stage=Stage.ACQUIRE, cluster=make_cluster(app_session))
    app_session.commit()

    report = run_batch(app_session, network=NETWORK, lease=LEASE)

    assert report == AcquireReport(claimed=1, failed=1)
    app_session.expire_all()
    (row,) = app_session.scalars(sa.select(PipelineWork)).all()
    assert row.claimed_by is None
    assert row.last_error is not None and "has a cluster subject" in row.last_error
