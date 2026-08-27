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
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import (
    CanonicalRecord,
    Cluster,
    ClusterProjection,
    ClusterProjectionSource,
    Summary,
)
from meridian.readmodel.project import _locked_cluster, project_cluster, rebuild
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


def test_summary_text_with_no_reason_is_rejected(app_session: Session) -> None:
    """The branch three-valued logic left open, and the one the constraint exists for.

    ⚠️ A CHECK rejects a row only when it evaluates to FALSE, and a comparison against a
    NULL ``withhold_reason`` is NULL rather than FALSE. Without the ``IS NOT NULL`` guard on
    each branch this row evaluates FALSE OR NULL OR FALSE — NULL — and is **accepted**: a
    projection carrying summary text while claiming no summary has been attempted.

    ``alembic check`` compares CHECK constraints by name only and cannot see the expression,
    so nothing but this test stands between that state and the reader.
    """
    cluster, _ = _single_source_cluster(app_session)

    with pytest.raises(IntegrityError, match="summary_matches_withhold_reason"):
        app_session.execute(
            sa.insert(ClusterProjection).values(
                cluster_id=cluster.cluster_id,
                headline="h",
                summary_text="text with no reason beside it",
                withhold_reason=None,
                article_count=1,
                distinct_source_count=1,
            )
        )


def test_a_withheld_projection_cannot_carry_summary_text(app_session: Session) -> None:
    """The read model is the last place a body we hold no rights to could reach a reader."""
    cluster, _ = _single_source_cluster(app_session)

    with pytest.raises(IntegrityError, match="summary_matches_withhold_reason"):
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


def test_a_tied_headline_does_not_depend_on_the_query_plan(app_session: Session) -> None:
    """The tie-break, which the rest of the corpus cannot exercise.

    ⚠️ ``_members`` claims its order must be total or the projection stops being rebuildable,
    and nothing could fail if that were false: every other cluster in the suite has members
    with distinct ``published_at``, so a corpus that cannot produce a tie cannot falsify the
    rule that handles ties. Same family as the MER-17 calibration corpus and the MER-18
    silver standard. Two articles at one instant is not exotic — a publisher's feed
    timestamps a batch identically, and a feed with no per-item date leaves them all NULL.

    ⚠️ Nor is it enough to project twice and compare: with no tie-break PostgreSQL still
    returns a small table in a repeatable order, so the wrong answer is repeatably wrong and
    an equality check passes. What actually distinguishes a total ordering is that the
    result does not depend on the **plan**. Forcing the join off its index reorders the
    input to the sort, and only a tie-break makes the output invariant under that. Measured
    before this test was written: with the tie-break deleted the two plans disagree.

    Membership is inserted in reverse id order so physical order and id order differ.
    """
    source = make_source(app_session, "BBC")
    cluster = make_cluster(app_session, article_count=12, distinct_source_count=1)
    articles = [
        make_article(
            app_session,
            source,
            guid=f"g{n:02d}",
            url=f"https://bbc.test/{n:02d}",
            title=f"title-{n:02d}",
            state=PipelineState.CLUSTERED,
            published_at=NOW,
        )
        for n in range(12)
    ]
    for article in reversed(articles):
        make_member(app_session, cluster, article)
    app_session.commit()

    project_cluster(app_session, cluster.cluster_id)
    default_plan = _projection(app_session, cluster.cluster_id)

    app_session.execute(sa.text("SET LOCAL enable_seqscan = off"))
    project_cluster(app_session, cluster.cluster_id)
    other_plan = _projection(app_session, cluster.cluster_id)

    assert default_plan is not None and other_plan is not None
    # The lowest article_id breaks the tie, and it is the first one inserted.
    assert default_plan.headline == "title-00"
    assert other_plan.headline == default_plan.headline


def test_one_publisher_covering_twice_at_one_instant_picks_a_stable_url(
    app_session: Session,
) -> None:
    """The same gap in ``_coverage``: its rank and timestamp both tie, so ``url`` decides.

    A publisher reissuing a story under a second guid in one batch produces exactly this.
    """
    source = make_source(app_session, "BBC")
    cluster = make_cluster(app_session, article_count=2, distinct_source_count=1)
    for guid, url in (("b", "https://bbc.test/z"), ("a", "https://bbc.test/a")):
        make_member(
            app_session,
            cluster,
            make_article(
                app_session,
                source,
                guid=guid,
                url=url,
                state=PipelineState.CLUSTERED,
                published_at=NOW,
            ),
        )

    project_cluster(app_session, cluster.cluster_id)
    first = _coverage(app_session, cluster.cluster_id)

    app_session.execute(sa.text("TRUNCATE cluster_projection, cluster_projection_source"))
    rebuild(app_session)

    assert first == [("BBC", "https://bbc.test/a")]
    assert _coverage(app_session, cluster.cluster_id) == first


def test_rebuild_counts_readable_rows_not_clusters_visited(app_session: Session) -> None:
    """An empty cluster is removed rather than projected, so the two numbers differ.

    ``rebuild``'s docstring says only the first answers what an operator is asking. Nothing
    asserted it, because no corpus in the suite contained a cluster with no members.
    """
    cluster, _ = _single_source_cluster(app_session)
    make_cluster(app_session)  # no members, therefore not readable
    app_session.flush()

    assert app_session.scalar(sa.select(sa.func.count()).select_from(Cluster)) == 2
    assert rebuild(app_session) == 1
    assert _projection(app_session, cluster.cluster_id) is not None


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
# Concurrency — being inside the transaction orders the writes, not the reads.
# --------------------------------------------------------------------------------------


def test_the_cluster_row_is_locked_rather_than_merely_read(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """Two workers must not project one cluster from snapshots taken before each other.

    ⚠️ Running inside the stage's transaction orders the *writes* and does nothing for the
    *reads*. Under READ COMMITTED both workers would read the write tables, then upsert, and
    the loser would overwrite the winner with an answer the write tables never justified —
    the observed shape being a summarized cluster whose projection says "not summarized
    yet", forever, with every work row discharged and ``pipeline_state`` reporting done.
    Two stage jobs on one scheduler reach this without any horizontal scaling; ``claim()``
    hands out disjoint *work rows*, and two articles of one cluster are two work rows.

    ⚠️ This calls the locking read directly, and that is the point. Calling
    ``project_cluster`` here proves nothing: ``cluster_projection`` has a foreign key to
    ``cluster``, and inserting a child row takes ``FOR KEY SHARE`` on the parent — so the
    upsert blocks against the holder whether or not we ever took a lock of our own. The
    first version of this test passed with ``FOR UPDATE`` deleted, measuring the foreign
    key's incidental lock at write time instead of ours at read time.

    ``lock_timeout`` turns "blocked" into a failure rather than a hung suite, and is set
    LOCAL so the pooled connection does not carry it into the next test.
    """
    cluster, _ = _single_source_cluster(app_session)
    cluster_id = cluster.cluster_id
    app_session.commit()

    holder = app_migrated.connect()
    try:
        holder.execute(
            sa.text("SELECT cluster_id FROM cluster WHERE cluster_id = :id FOR UPDATE"),
            {"id": cluster_id},
        )
        with Session(app_migrated, expire_on_commit=False) as other:
            other.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                _locked_cluster(other, cluster_id)
    finally:
        holder.rollback()
        holder.close()


def test_the_lock_is_taken_before_anything_else_is_read(app_session: Session) -> None:
    """A lock acquired after a read protects nothing that has already been read.

    The ordering *is* the correctness argument, so it is asserted rather than left to the
    order the statements happen to sit in. Move the locking read below the member query and
    the race is back with the lock still present and looking right.
    """
    cluster, _ = _single_source_cluster(app_session)
    app_session.commit()

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(" ".join(statement.split()))

    engine = app_session.get_bind()
    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        project_cluster(app_session, cluster.cluster_id)
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)

    reads = [text for text in statements if text.upper().startswith("SELECT")]
    assert reads, "the projection read nothing, so this asserts nothing"
    assert "FOR UPDATE" in reads[0], f"first read was not the lock: {reads[0]}"
    assert "FROM cluster " in reads[0]


def test_the_summary_is_read_from_the_database_not_the_session(app_session: Session) -> None:
    """The summarize stage is trigger two's only caller, and it has just written this row.

    ⚠️ A Core upsert — the natural way to write a row that may not exist — leaves any
    instance already in the identity map stale, and ``session.get`` would then hand back the
    pre-write text for the projection to publish. No error, no warning: the read model
    quietly serves an older summary than the one the database holds.
    """
    cluster, _ = _single_source_cluster(app_session)
    make_summary(app_session, cluster, text="draft one")
    app_session.commit()

    # Load it into the identity map the way a handler that inspected it first would.
    stale = app_session.get(Summary, cluster.cluster_id)
    assert stale is not None and stale.text == "draft one"

    # ⚠️ The write has to be the shape the summarize stage will actually use: an upsert,
    # because the row may not exist. A plain ``sa.update()`` on the mapped class is
    # ORM-enabled and synchronizes the session, so the identity map is refreshed and this
    # test then passes whatever the projection reads — the first version did exactly that
    # and proved nothing.
    app_session.execute(
        pg_insert(Summary)
        .values(cluster_id=cluster.cluster_id, text="final text")
        .on_conflict_do_update(index_elements=[Summary.cluster_id], set_={"text": "final text"})
    )
    assert (
        app_session.scalar(sa.select(Summary.text).where(Summary.cluster_id == cluster.cluster_id))
        == "final text"
    ), "the database did not take the write, so the rest of this test is vacuous"

    project_cluster(app_session, cluster.cluster_id)

    row = _projection(app_session, cluster.cluster_id)
    assert row is not None
    assert row.summary_text == "final text"


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
