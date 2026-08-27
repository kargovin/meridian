"""Recomputing a cluster's read-model rows from the write tables (RFC §6.3, FR-V1).

The projection is continuous and inline: a stage completing projects what it made readable,
in the transaction that completes it. There is no scheduled rebuild driving the surface — a
full-rebuild cadence would spend the freshness budget (FR-I1) that the whole pipeline is
built around.

Two triggers, not one gate, and the difference is load-bearing:

* an article reaching ``PROJECTABLE_STATE`` — it now has a topic and a cluster, which is
  everything browse needs;
* a cluster's summary changing — the text or the reason it is absent.

Neither is "the pipeline finished with this cluster". Under FR-S6 a cluster is summarized
only at >= 2 distinct sources, so most clusters never are, and a cluster withheld under
FR-S2 or FR-S5 is finished and must still be readable. Gating on a summary would empty the
surface in a way that looks exactly like a broken pipeline.

:func:`project_cluster` is the only thing that writes these tables, and :func:`rebuild`
calls it in a loop rather than issuing bulk SQL of its own. That is deliberate: the
rebuildability property is that a truncated read model comes back identical, and two
implementations of one rule agree until the day somebody edits one of them.

⚠️ The writes here are Core statements and do not synchronize the session's identity map. A
``ClusterProjection`` instance loaded before a re-projection keeps answering with the values
it was loaded with, and nothing raises. Read these tables through a query, or a fresh
session, rather than through an instance you were already holding.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from meridian.db.models import (
    AlternateCopy,
    CanonicalRecord,
    Cluster,
    ClusterMember,
    ClusterProjection,
    ClusterProjectionSource,
    Source,
    Summary,
)

log = logging.getLogger(__name__)


def _members(session: Session, cluster_id: int) -> Sequence[CanonicalRecord]:
    """The cluster's member articles, earliest publication first.

    The order is the headline rule and has to be total, or two projections of one cluster
    disagree and the read model stops being rebuildable. ``published_at`` is nullable —
    plenty of feeds omit it — so NULLs sort last and ``article_id`` breaks every remaining
    tie.
    """
    return (
        session.execute(
            sa.select(CanonicalRecord)
            .join(ClusterMember, ClusterMember.article_id == CanonicalRecord.article_id)
            .where(ClusterMember.cluster_id == cluster_id)
            .order_by(
                sa.nulls_last(CanonicalRecord.published_at.asc()),
                CanonicalRecord.article_id.asc(),
            )
        )
        .scalars()
        .all()
    )


def _coverage(session: Session, article_ids: Sequence[int]) -> Sequence[tuple[int, str, str]]:
    """``(source_id, source_name, url)`` per distinct publisher covering these articles.

    A publisher contributes through a member article or through a copy of one that dedup
    collapsed, and both count — a collapsed copy is the second outlet to run the story,
    which is the provenance FR-I5 keeps rather than deletes. One publisher covering the
    story twice is one entry, matching the identity FR-S6 counts.

    ⚠️ A member article and a collapsed copy are timestamped by different clocks —
    ``published_at`` is when the publisher published, ``seen_at`` is when we noticed the
    duplicate — so ordering the two together compares quantities that are not comparable.
    ``rank`` settles it before the timestamps are consulted: where a publisher appears both
    ways, the member wins, because that is the record we actually hold and the one the
    reader should be sent to. Timestamps then only ever order contributions of one kind.
    """
    if not article_ids:
        return []
    members = sa.select(
        CanonicalRecord.source_id,
        CanonicalRecord.url_canonical.label("url"),
        CanonicalRecord.published_at.label("at"),
        sa.literal(0).label("rank"),
    ).where(CanonicalRecord.article_id.in_(article_ids))
    copies = sa.select(
        AlternateCopy.source_id,
        AlternateCopy.url.label("url"),
        AlternateCopy.seen_at.label("at"),
        sa.literal(1).label("rank"),
    ).where(AlternateCopy.article_id.in_(article_ids))
    contributions = sa.union_all(members, copies).subquery()

    # DISTINCT ON keeps one row per publisher; the ORDER BY inside it is what picks which,
    # so it must be total for the same reason _members' is. url is the final tie-break
    # because a publisher's two items cannot share one.
    earliest = (
        sa.select(contributions)
        .distinct(contributions.c.source_id)
        .order_by(
            contributions.c.source_id,
            contributions.c.rank.asc(),
            sa.nulls_last(contributions.c.at.asc()),
            contributions.c.url.asc(),
        )
        .subquery()
    )
    return [
        (source_id, name, url)
        for source_id, name, url in session.execute(
            sa.select(earliest.c.source_id, Source.name, earliest.c.url)
            .join(Source, Source.source_id == earliest.c.source_id)
            .order_by(earliest.c.source_id)
        ).all()
    ]


def _locked_cluster(session: Session, cluster_id: int) -> Cluster | None:
    """The cluster row, locked for the rest of the transaction.

    ``FOR UPDATE`` here serializes projections of one cluster: a second worker blocks until
    the first commits, and then — READ COMMITTED taking a fresh snapshot per statement —
    reads everything the first wrote. Taken before any other read, because a lock acquired
    afterwards protects nothing that has already been read.

    ``populate_existing`` because the identity map would otherwise answer from an instance
    loaded earlier in this transaction, which is both stale and unlocked.
    """
    return session.scalars(
        sa.select(Cluster)
        .where(Cluster.cluster_id == cluster_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def _current_summary(session: Session, cluster_id: int) -> Summary | None:
    """The summary as the database holds it, not as the session remembers it.

    ⚠️ The caller is the summarize stage, so this row is the one most likely to have just
    been written — and a Core upsert, which is the natural way to write a row that may not
    exist, leaves any instance already in the identity map stale. ``session.get`` would then
    return the pre-write text and the projection would publish it, with no error.
    """
    return session.scalars(
        sa.select(Summary)
        .where(Summary.cluster_id == cluster_id)
        .execution_options(populate_existing=True)
    ).one_or_none()


def project_cluster(session: Session, cluster_id: int) -> None:
    """Recompute one cluster's projection rows from the write tables.

    Writes to the session and commits nothing: the caller's single commit is what makes the
    projection part of the same transaction as the stage that caused it. Committing here
    would open a window in which an article is ``clustered`` with no projection row — and
    nothing could find it afterwards, because §6.3's reconciler derives owed work from
    ``pipeline_state``, which says the article is done.

    A cluster with no members is removed rather than left behind. Reconciliation moves
    membership between clusters, so a cluster can legitimately empty out, and an empty one
    has no headline to render.

    ⚠️ The cluster row is locked before anything is read, and the lock is what makes this
    safe to run concurrently. Being inside the caller's transaction orders the *writes* and
    does nothing for the *reads*: under READ COMMITTED two workers projecting one cluster
    would each compute from a snapshot taken before the other committed, and the loser would
    overwrite the winner with an answer the write tables never justified. The observed
    result is a cluster whose summary exists and whose projection says "not summarized yet",
    permanently — every work row discharged, ``pipeline_state`` reporting done, so §6.3's
    reconciler cannot see it either. Two stage jobs sharing one scheduler is enough to reach
    it; ``claim()``'s ``SKIP LOCKED`` gives workers disjoint *work rows*, which is not the
    same as disjoint *clusters*.
    """
    cluster = _locked_cluster(session, cluster_id)
    if cluster is None:
        raise ValueError(f"cannot project cluster {cluster_id}, which is gone")

    members = _members(session, cluster_id)
    if not members:
        session.execute(
            sa.delete(ClusterProjection).where(ClusterProjection.cluster_id == cluster_id)
        )
        return

    head = members[0]
    summary = _current_summary(session, cluster_id)
    coverage = _coverage(session, [article.article_id for article in members])

    row = {
        "topic": cluster.representative_topic,
        "headline": head.title,
        "lede": head.lede,
        "summary_text": summary.text if summary is not None else None,
        # NULL is "no summary row", which is not the same fact as 'none'.
        "withhold_reason": summary.withhold_reason if summary is not None else None,
        "article_count": cluster.article_count,
        # Copied, never counted from the coverage rows below. FR-S6 gates summarization on
        # the write model's number; a read model that computed its own could show a second
        # source for a cluster the pipeline treats as single-source.
        "distinct_source_count": cluster.distinct_source_count,
        "earliest_at": cluster.earliest_at,
        "latest_at": cluster.latest_at,
    }
    # One dict for both halves of the upsert: an insert and an update that can disagree is
    # a row whose columns depend on whether the cluster had been projected before.
    session.execute(
        insert(ClusterProjection)
        .values(cluster_id=cluster_id, **row)
        .on_conflict_do_update(index_elements=[ClusterProjection.cluster_id], set_=row)
    )

    # Replaced wholesale rather than diffed. The set is a handful of rows, and a publisher
    # can leave a cluster when reconciliation moves an article, so an upsert alone would
    # leave the departed one behind.
    session.execute(
        sa.delete(ClusterProjectionSource).where(ClusterProjectionSource.cluster_id == cluster_id)
    )
    if coverage:
        session.execute(
            sa.insert(ClusterProjectionSource),
            [
                {
                    "cluster_id": cluster_id,
                    "source_id": source_id,
                    "source_name": name,
                    "url": url,
                }
                for source_id, name, url in coverage
            ],
        )


def project_article_cluster(session: Session, article_id: int) -> None:
    """Project the cluster this article belongs to.

    An article at ``PROJECTABLE_STATE`` with no membership row is a defect in whatever put
    it there, not a state to skip past: it would be invisible to readers and to §6.3's
    reconciler alike, since ``pipeline_state`` reports it as finished. Refused loudly for
    the same reason ``advance()`` refuses a mis-subjected work row.
    """
    cluster_id = session.scalar(
        sa.select(ClusterMember.cluster_id).where(ClusterMember.article_id == article_id)
    )
    if cluster_id is None:
        raise ValueError(
            f"article {article_id} is projectable but belongs to no cluster; "
            "nothing would ever make it readable"
        )
    project_cluster(session, cluster_id)


def rebuild(session: Session) -> int:
    """Re-project every cluster from the write tables. Returns how many rows are readable.

    Housekeeping, not the path (RFC §6.3) — the primary mechanism is the inline projection,
    and this exists so a read model that has drifted can be made right without a reingest.
    Nothing schedules it here.

    The count is readable rows rather than clusters visited: a cluster with no members is
    removed by ``project_cluster`` rather than projected, so the two numbers differ and only
    the first answers what an operator is asking.

    Commits nothing. Truncate both projection tables, call this, and the rows come back
    identical; that equality is what keeps the read model a cache rather than a second
    source of truth.
    """
    cluster_ids = session.execute(sa.select(Cluster.cluster_id).order_by(Cluster.cluster_id))
    for (cluster_id,) in cluster_ids.all():
        project_cluster(session, cluster_id)
    readable = session.scalar(sa.select(sa.func.count()).select_from(ClusterProjection))
    return readable or 0
