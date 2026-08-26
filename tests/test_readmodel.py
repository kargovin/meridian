"""The read-model projection (RFC §6.3, FR-V1).

The three acceptance criteria this story is written against:

* a cluster is readable before it has a summary,
* the projection is rebuildable from the write tables,
* ``withhold_reason`` survives it, all three values distinctly.

Plus the two triggers, because a projection nothing calls is a table.
"""

import datetime as dt
from typing import Any

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage, WithholdReason
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import (
    CanonicalRecord,
    Cluster,
    ClusterProjection,
    ClusterProjectionSource,
)
from meridian.readmodel.project import project_cluster, rebuild
from tests.factories import (
    make_alternate_copy,
    make_article,
    make_cluster,
    make_member,
    make_source,
    make_summary,
    make_work,
)

NOW = dt.datetime(2026, 8, 26, 12, 0, tzinfo=dt.UTC)


def _projection(app_session: Session, cluster_id: int) -> sa.Row[Any] | None:
    """The projection row as the database holds it.

    Deliberately not ``session.get``: the projection is written with Core statements, which
    do not synchronize the session's identity map, so an instance loaded before a
    re-projection keeps answering with the values it was loaded with. A test that asked the
    session would pass while the read model never changed.
    """
    return app_session.execute(
        sa.select(*ClusterProjection.__table__.c).where(ClusterProjection.cluster_id == cluster_id)
    ).one_or_none()


def _coverage(app_session: Session, cluster_id: int) -> list[tuple[str, str]]:
    """``(publisher name, url)`` for a cluster, in a stable order."""
    return [
        (name, url)
        for name, url in app_session.execute(
            sa.select(ClusterProjectionSource.source_name, ClusterProjectionSource.url)
            .where(ClusterProjectionSource.cluster_id == cluster_id)
            .order_by(ClusterProjectionSource.source_name)
        ).all()
    ]


def _single_source_cluster(app_session: Session) -> tuple[Cluster, CanonicalRecord]:
    """One publisher, one article, clustered."""
    source = make_source(app_session, "BBC")
    article = make_article(
        app_session,
        source,
        guid="bbc-1",
        title="Chip export controls tighten",
        lede="Ministers confirmed the new rules on Tuesday.",
        state=PipelineState.CLUSTERED,
        published_at=NOW,
    )
    cluster = make_cluster(
        app_session,
        representative_topic="Technology",
        article_count=1,
        distinct_source_count=1,
        earliest_at=NOW,
        latest_at=NOW,
    )
    make_member(app_session, cluster, article)
    return cluster, article


# --------------------------------------------------------------------------------------
# AC1 — a cluster is readable before it has a summary.
# --------------------------------------------------------------------------------------


def test_a_cluster_with_no_summary_is_readable(app_session: Session) -> None:
    """Gate on a summary and single-source clusters never appear at all.

    Under FR-S6 a cluster is summarized only at >= 2 distinct sources, so most clusters
    never are. Waiting for one empties the surface in a way indistinguishable from a broken
    pipeline, and leaves the "show the article's own lede instead" render with nothing to
    render — which is why the lede is projected too.
    """
    cluster, _ = _single_source_cluster(app_session)

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.topic == "Technology"
    assert row.headline == "Chip export controls tighten"
    assert row.lede == "Ministers confirmed the new rules on Tuesday."
    assert row.summary_text is None
    # NULL, not 'none': no summary row exists, which is a different fact from summarized
    # and not withheld.
    assert row.withhold_reason is None


def test_a_summary_appearing_later_reaches_the_projection(app_session: Session) -> None:
    """The second trigger: the same cluster re-projects when its summary lands."""
    cluster, _ = _single_source_cluster(app_session)
    project_cluster(app_session, cluster.cluster_id)
    before = _projection(app_session, cluster.cluster_id)
    assert before is not None and before.summary_text is None

    make_summary(
        app_session, cluster, text="Ministers tightened export controls on advanced chips."
    )
    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.summary_text == "Ministers tightened export controls on advanced chips."
    assert row.withhold_reason is WithholdReason.NONE


# --------------------------------------------------------------------------------------
# AC3 — withhold_reason survives, all three values distinctly.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        WithholdReason.BELOW_FAITHFULNESS_BAR,
        WithholdReason.RIGHTS_EXCLUDED,
        WithholdReason.INSUFFICIENT_SOURCES,
    ],
)
def test_each_withhold_reason_survives_the_projection(
    app_session: Session, reason: WithholdReason
) -> None:
    """ "No summary" is four different facts and the reader surface has to tell them apart.

    Collapse them to a boolean and a rights exclusion becomes indistinguishable from a
    transient one-source cluster that will resolve itself on the next arrival.
    """
    cluster, _ = _single_source_cluster(app_session)
    make_summary(app_session, cluster, withhold_reason=reason)

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.withhold_reason is reason
    assert row.summary_text is None


def test_the_four_no_summary_states_are_distinguishable(app_session: Session) -> None:
    """The whole point of the nullable enum, asserted in one place.

    Not summarized yet, summarized fine, and each withheld reason must be four different
    readings of one column.
    """
    source = make_source(app_session, "BBC")
    seen: list[WithholdReason | None] = []
    cases: list[WithholdReason | None] = [
        None,
        WithholdReason.NONE,
        WithholdReason.BELOW_FAITHFULNESS_BAR,
        WithholdReason.RIGHTS_EXCLUDED,
        WithholdReason.INSUFFICIENT_SOURCES,
    ]
    for n, reason in enumerate(cases):
        article = make_article(
            app_session, source, guid=f"g-{n}", state=PipelineState.CLUSTERED, published_at=NOW
        )
        cluster = make_cluster(app_session, article_count=1, distinct_source_count=1)
        make_member(app_session, cluster, article)
        if reason is not None:
            make_summary(
                app_session,
                cluster,
                text="a summary" if reason is WithholdReason.NONE else None,
                withhold_reason=reason,
            )
        project_cluster(app_session, cluster.cluster_id)
        row = _projection(app_session, cluster.cluster_id)
        assert row is not None
        seen.append(row.withhold_reason)

    assert seen == cases
    assert len(set(seen)) == 5


def test_a_withheld_projection_cannot_carry_summary_text(app_session: Session) -> None:
    """The read model is the last place a body we hold no rights to could reach a reader."""
    cluster, _ = _single_source_cluster(app_session)

    with pytest.raises(IntegrityError):
        app_session.execute(
            sa.insert(ClusterProjection).values(
                cluster_id=cluster.cluster_id,
                headline="h",
                summary_text="the body we were told not to publish",
                withhold_reason=WithholdReason.RIGHTS_EXCLUDED,
                article_count=1,
                distinct_source_count=1,
            )
        )


# --------------------------------------------------------------------------------------
# The coverage list.
# --------------------------------------------------------------------------------------


def test_coverage_lists_publishers_not_articles(app_session: Session) -> None:
    """One publisher running two pieces on one story is one entry.

    Per-publisher is the identity FR-S6 counts, and it is what "also reported by" means.
    """
    bbc = make_source(app_session, "BBC")
    cluster = make_cluster(app_session, article_count=2, distinct_source_count=1)
    for n in (1, 2):
        article = make_article(
            app_session,
            bbc,
            guid=f"bbc-{n}",
            url=f"https://bbc.test/{n}",
            state=PipelineState.CLUSTERED,
            published_at=NOW + dt.timedelta(minutes=n),
        )
        make_member(app_session, cluster, article)

    project_cluster(app_session, cluster.cluster_id)

    # The earliest of the publisher's two items, by the same rule as the headline.
    assert _coverage(app_session, cluster.cluster_id) == [("BBC", "https://bbc.test/1")]


def test_a_collapsed_duplicate_still_counts_as_coverage(app_session: Session) -> None:
    """An AlternateCopy is a publisher whose article dedup collapsed, not one that vanished.

    Dropping it here loses exactly the provenance collapse-not-drop exists to keep (FR-I5).
    """
    bbc = make_source(app_session, "BBC")
    npr = make_source(app_session, "NPR")
    article = make_article(
        app_session, bbc, guid="bbc-1", state=PipelineState.CLUSTERED, published_at=NOW
    )
    make_alternate_copy(app_session, article, npr, guid="npr-1", url="https://npr.test/1")
    cluster = make_cluster(app_session, article_count=1, distinct_source_count=2)
    make_member(app_session, cluster, article)

    project_cluster(app_session, cluster.cluster_id)

    assert _coverage(app_session, cluster.cluster_id) == [
        ("BBC", "https://example.test/bbc-1"),
        ("NPR", "https://npr.test/1"),
    ]


def test_a_publisher_present_both_ways_links_to_its_member_article(app_session: Session) -> None:
    """One publisher can be both a cluster member and a collapsed duplicate of another.

    ⚠️ The two are timestamped by different clocks — ``published_at`` for the member,
    ``seen_at`` for the copy — so picking "the earliest" across both compares quantities
    that are not comparable, and the winner depends on when we happened to poll. The member
    wins on rank before any timestamp is read, and it is also the record we actually hold.
    """
    bbc = make_source(app_session, "BBC")
    guardian = make_source(app_session, "Guardian")
    cluster = make_cluster(app_session, article_count=1, distinct_source_count=2)
    member = make_article(
        app_session,
        guardian,
        guid="gu-1",
        url="https://guardian.test/1",
        state=PipelineState.CLUSTERED,
        # Published long after the BBC copy below was seen, so a naive "earliest across
        # both" would hand BBC the collapsed URL instead of its own member article.
        published_at=NOW + dt.timedelta(days=1),
    )
    make_member(app_session, cluster, member)
    bbc_member = make_article(
        app_session,
        bbc,
        guid="bbc-own",
        url="https://bbc.test/own",
        state=PipelineState.CLUSTERED,
        published_at=NOW + dt.timedelta(days=1),
    )
    make_member(app_session, cluster, bbc_member)
    make_alternate_copy(app_session, member, bbc, guid="bbc-dupe", url="https://bbc.test/collapsed")

    project_cluster(app_session, cluster.cluster_id)

    assert ("BBC", "https://bbc.test/own") in _coverage(app_session, cluster.cluster_id)


def test_a_publisher_leaving_the_cluster_leaves_the_coverage_list(app_session: Session) -> None:
    """Reconciliation moves articles between clusters, so coverage shrinks as well as grows.

    Upserting alone would leave the departed publisher behind, which is the failure that
    reads as "we cited an outlet that never ran this".
    """
    bbc = make_source(app_session, "BBC")
    guardian = make_source(app_session, "Guardian")
    cluster = make_cluster(app_session, article_count=2, distinct_source_count=2)
    articles = []
    for n, source in enumerate((bbc, guardian)):
        article = make_article(
            app_session,
            source,
            guid=f"g-{n}",
            url=f"https://{source.name.lower()}.test/1",
            state=PipelineState.CLUSTERED,
            published_at=NOW + dt.timedelta(minutes=n),
        )
        make_member(app_session, cluster, article)
        articles.append(article)
    project_cluster(app_session, cluster.cluster_id)
    assert len(_coverage(app_session, cluster.cluster_id)) == 2

    app_session.execute(
        sa.text("DELETE FROM cluster_member WHERE article_id = :a"),
        {"a": articles[1].article_id},
    )
    project_cluster(app_session, cluster.cluster_id)

    assert _coverage(app_session, cluster.cluster_id) == [("BBC", "https://bbc.test/1")]


# --------------------------------------------------------------------------------------
# The headline rule.
# --------------------------------------------------------------------------------------


def test_the_headline_is_the_earliest_member(app_session: Session) -> None:
    """Earliest, for the same reason the first arrival is the canonical record.

    A cluster is an event and the first report defines it; taking the latest would mutate
    the browse row's title under a reader every time another outlet piles on.
    """
    bbc = make_source(app_session, "BBC")
    guardian = make_source(app_session, "Guardian")
    cluster = make_cluster(app_session, article_count=2, distinct_source_count=2)
    late = make_article(
        app_session,
        guardian,
        guid="g-late",
        title="Analysis: what the controls mean",
        state=PipelineState.CLUSTERED,
        published_at=NOW + dt.timedelta(hours=3),
    )
    early = make_article(
        app_session,
        bbc,
        guid="b-early",
        title="Chip export controls tighten",
        state=PipelineState.CLUSTERED,
        published_at=NOW,
    )
    make_member(app_session, cluster, late)
    make_member(app_session, cluster, early)

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.headline == "Chip export controls tighten"


def test_articles_without_a_publication_date_sort_last(app_session: Session) -> None:
    """``published_at`` is nullable — plenty of feeds omit it — and the order must stay total.

    A NULL sorting first would hand the headline to whichever article happened to lack a
    date, and two projections of one cluster could disagree.

    ⚠️ This asserts the behaviour, not the ``nulls_last`` wrapper: PostgreSQL already sorts
    NULLs last for ASC (measured), so removing the wrapper leaves this green. It earns its
    place against a later switch to DESC, where the default flips to NULLS FIRST and an
    undated article would take the headline — nothing else would catch that.
    """
    source = make_source(app_session, "BBC")
    cluster = make_cluster(app_session, article_count=2, distinct_source_count=1)
    undated = make_article(
        app_session, source, guid="undated", title="Undated", state=PipelineState.CLUSTERED
    )
    dated = make_article(
        app_session,
        source,
        guid="dated",
        title="Dated",
        state=PipelineState.CLUSTERED,
        published_at=NOW,
    )
    make_member(app_session, cluster, undated)
    make_member(app_session, cluster, dated)

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.headline == "Dated"


# --------------------------------------------------------------------------------------
# AC2 — the projection is rebuildable.
# --------------------------------------------------------------------------------------


def _snapshot(app_session: Session) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Every column of every projection row, in a deterministic order.

    Columns rather than entities: comparing ORM instances compares identity, so two
    snapshots of different rows would be unequal for the wrong reason and — worse — two
    snapshots of the *same* stale instances would be equal for the wrong reason.
    """
    parents = [
        tuple(row)
        for row in app_session.execute(
            sa.select(*ClusterProjection.__table__.c).order_by(ClusterProjection.cluster_id)
        ).all()
    ]
    children = [
        tuple(row)
        for row in app_session.execute(
            sa.select(*ClusterProjectionSource.__table__.c).order_by(
                ClusterProjectionSource.cluster_id, ClusterProjectionSource.source_id
            )
        ).all()
    ]
    return parents, children


def _varied_corpus(app_session: Session) -> None:
    """Clusters spanning every state the projection can hold, all projected inline."""
    bbc = make_source(app_session, "BBC")
    guardian = make_source(app_session, "Guardian")
    npr = make_source(app_session, "NPR")

    states: list[WithholdReason | None] = [
        None,
        WithholdReason.NONE,
        WithholdReason.BELOW_FAITHFULNESS_BAR,
        WithholdReason.RIGHTS_EXCLUDED,
        WithholdReason.INSUFFICIENT_SOURCES,
    ]
    for n, reason in enumerate(states):
        cluster = make_cluster(
            app_session,
            representative_topic="Technology" if n % 2 else None,
            article_count=2,
            distinct_source_count=3,
            earliest_at=NOW,
            latest_at=NOW + dt.timedelta(hours=n),
        )
        first = make_article(
            app_session,
            bbc,
            guid=f"bbc-{n}",
            url=f"https://bbc.test/{n}",
            title=f"Story {n}",
            lede=f"Lede {n}",
            state=PipelineState.CLUSTERED,
            published_at=NOW,
        )
        second = make_article(
            app_session,
            guardian,
            guid=f"gu-{n}",
            url=f"https://guardian.test/{n}",
            title=f"Analysis {n}",
            state=PipelineState.CLUSTERED,
            published_at=NOW + dt.timedelta(hours=1),
        )
        make_alternate_copy(app_session, first, npr, guid=f"npr-{n}", url=f"https://npr.test/{n}")
        make_member(app_session, cluster, first)
        make_member(app_session, cluster, second)
        if reason is not None:
            make_summary(
                app_session,
                cluster,
                text=f"Summary {n}" if reason is WithholdReason.NONE else None,
                withhold_reason=reason,
            )
        project_cluster(app_session, cluster.cluster_id)


def test_the_projection_is_rebuildable(app_session: Session) -> None:
    """Truncate the read model, rebuild it from the write tables, get identical rows back.

    This is what makes the denormalization safe. If the rebuild cannot reproduce the rows,
    the cache holds a fact nothing else does — it has become a source of truth, and the
    self-healing re-projection has nothing to heal *from*. The failure is silent: a
    projection with one wrong column looks exactly like a correct one.
    """
    _varied_corpus(app_session)
    app_session.flush()
    before = _snapshot(app_session)
    assert before[0], "the corpus projected nothing, so the comparison would be vacuous"
    assert before[1]

    app_session.execute(sa.text("TRUNCATE cluster_projection, cluster_projection_source"))
    assert _snapshot(app_session) == ([], [])

    assert rebuild(app_session) == len(before[0])
    app_session.flush()

    assert _snapshot(app_session) == before


def test_rebuild_removes_a_cluster_that_lost_its_members(app_session: Session) -> None:
    """An empty cluster has no headline to render, so it is not readable.

    Reconciliation can legitimately empty a cluster out by moving its articles elsewhere.
    """
    cluster, article = _single_source_cluster(app_session)
    project_cluster(app_session, cluster.cluster_id)
    assert _projection(app_session, cluster.cluster_id) is not None

    app_session.execute(
        sa.text("DELETE FROM cluster_member WHERE article_id = :a"), {"a": article.article_id}
    )
    project_cluster(app_session, cluster.cluster_id)

    assert _projection(app_session, cluster.cluster_id) is None
    assert _coverage(app_session, cluster.cluster_id) == []


# --------------------------------------------------------------------------------------
# The triggers — a projection nothing calls is a table.
# --------------------------------------------------------------------------------------


def test_completing_the_cluster_stage_projects(app_session: Session) -> None:
    """Trigger one, through the helper every stage discharges itself with.

    It lives in ``advance()`` rather than in the handler because the projection has to be
    in the same transaction as the state move: written after the commit, a crash in between
    leaves an article ``clustered`` and unreadable, and nothing can find it — the
    reconciler derives owed work from ``pipeline_state``, which says it is done.
    """
    source = make_source(app_session, "BBC")
    article = make_article(
        app_session, source, guid="bbc-1", state=PipelineState.CLASSIFIED, published_at=NOW
    )
    cluster = make_cluster(app_session, article_count=1, distinct_source_count=1)
    make_member(app_session, cluster, article)
    work = make_work(app_session, stage=Stage.CLUSTER, article=article)

    work_queue.advance(app_session, work)
    app_session.commit()

    assert article.pipeline_state is PipelineState.CLUSTERED
    assert _projection(app_session, cluster.cluster_id) is not None


def test_completing_a_summarize_row_reprojects_its_cluster(app_session: Session) -> None:
    """Trigger two. The summarize stage is the only one whose subject is a cluster."""
    cluster, _ = _single_source_cluster(app_session)
    project_cluster(app_session, cluster.cluster_id)
    app_session.commit()
    before = _projection(app_session, cluster.cluster_id)
    assert before is not None and before.summary_text is None

    make_summary(app_session, cluster, text="A summary.")
    work = make_work(app_session, stage=Stage.SUMMARIZE, cluster=cluster)

    work_queue.advance(app_session, work)
    app_session.commit()

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None and row.summary_text == "A summary."


def test_a_failed_projection_rolls_back_the_stage(app_session: Session) -> None:
    """The projection is inside the stage's transaction, not after it.

    ⚠️ This is the failure the placement exists to prevent, and it is invisible when it
    happens: an article left ``clustered`` with no projection row is finished and
    unreadable, and nothing can find it — §6.3's reconciler derives owed work from
    ``pipeline_state``, which reports it as done. Here the projection is made to fail; the
    state must not have moved.
    """
    source = make_source(app_session, "BBC")
    article = make_article(
        app_session, source, guid="bbc-1", state=PipelineState.CLASSIFIED, published_at=NOW
    )
    cluster = make_cluster(app_session, article_count=1, distinct_source_count=1)
    make_member(app_session, cluster, article)
    work = make_work(app_session, stage=Stage.CLUSTER, article=article)
    app_session.commit()

    def explode(*_: object, **__: object) -> None:
        raise RuntimeError("projection failed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(work_queue, "project_article_cluster", explode)
        with pytest.raises(RuntimeError):
            work_queue.advance(app_session, work)
    app_session.rollback()

    app_session.expire_all()
    survivor = app_session.get(CanonicalRecord, article.article_id)
    assert survivor is not None
    assert survivor.pipeline_state is PipelineState.CLASSIFIED
    assert _projection(app_session, cluster.cluster_id) is None


def test_the_distinct_source_count_is_copied_not_counted(app_session: Session) -> None:
    """FR-S6 gates summarization on the write model's number, so the read model copies it.

    Counted from the coverage rows instead, the two could disagree — and the reader would
    be told a cluster has two sources while the pipeline treats it as single-source and
    withholds its summary. The projection is a cache; it holds no fact of its own.
    """
    source = make_source(app_session, "BBC")
    article = make_article(
        app_session, source, guid="bbc-1", state=PipelineState.CLUSTERED, published_at=NOW
    )
    # One publisher covering it, but the write model has not counted yet: the default.
    cluster = make_cluster(app_session, article_count=1, distinct_source_count=0)
    make_member(app_session, cluster, article)

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.distinct_source_count == 0
    assert len(_coverage(app_session, cluster.cluster_id)) == 1


def test_an_earlier_stage_does_not_project(app_session: Session) -> None:
    """Only ``PROJECTABLE_STATE`` makes an article readable.

    Projecting at ``acquired`` would publish an article with no topic and no cluster — and
    ``project_article_cluster`` would refuse it, so the acquire stage would start failing.
    """
    source = make_source(app_session, "BBC")
    article = make_article(app_session, source, guid="bbc-1", state=PipelineState.DISCOVERED)
    work = make_work(app_session, stage=Stage.ACQUIRE, article=article)

    work_queue.advance(app_session, work)
    app_session.commit()

    assert article.pipeline_state is PipelineState.ACQUIRED
    assert app_session.scalar(sa.select(sa.func.count()).select_from(ClusterProjection)) == 0


def test_a_projectable_article_with_no_cluster_is_refused(app_session: Session) -> None:
    """It would be finished, unreadable, and invisible to the reconciler.

    ``pipeline_state`` reports it as done, so nothing would ever enqueue it again. Refused
    at the writer for the same reason ``advance()`` refuses a mis-subjected work row.
    """
    source = make_source(app_session, "BBC")
    article = make_article(app_session, source, guid="bbc-1", state=PipelineState.CLASSIFIED)
    work = make_work(app_session, stage=Stage.CLUSTER, article=article)

    with pytest.raises(ValueError, match="belongs to no cluster"):
        work_queue.advance(app_session, work)
