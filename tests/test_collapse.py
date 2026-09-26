"""Collapsing a duplicate into its representative (FR-I5, RFC §5.2)."""

import datetime as dt
import threading
import time

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage
from sqlalchemy.orm import Session

from meridian.db import work_queue
from meridian.db.models import (
    AlternateCopy,
    CanonicalRecord,
    ClusterProjectionSource,
    PipelineWork,
)
from tests.factories import (
    make_alternate_copy,
    make_article,
    make_cluster,
    make_feed,
    make_member,
    make_source,
    make_work,
)

pytestmark = pytest.mark.postgres

PUBLISHED = dt.datetime(2026, 9, 20, 8, 30, tzinfo=dt.UTC)


def test_the_duplicate_becomes_a_note_on_the_representative(app_session: Session) -> None:
    """The record goes, the publisher stays — collapse-not-drop (US-K2)."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    feed = make_feed(app_session, other)
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(
        app_session,
        other,
        guid="dupe",
        url="https://opendemocracy.test/dupe",
        state=PipelineState.ACQUIRED,
        feed_id=feed.feed_id,
        published_at=PUBLISHED,
    )
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()
    duplicate_id = duplicate.article_id

    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    assert app_session.get(CanonicalRecord, duplicate_id) is None
    copy = app_session.scalars(sa.select(AlternateCopy)).one()
    assert copy.article_id == representative.article_id
    assert copy.source_id == other.source_id
    assert copy.url == "https://opendemocracy.test/dupe"
    assert copy.guid == "dupe"
    assert copy.feed_id == feed.feed_id


def test_the_note_carries_the_copys_own_publication_date(app_session: Session) -> None:
    """``seen_at`` is when we noticed; ``published_at`` is when the publisher published, and
    the record that held it is being deleted. A copy published before its representative
    carries a date that exists nowhere else afterwards."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(
        app_session, other, guid="dupe", state=PipelineState.ACQUIRED, published_at=PUBLISHED
    )
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()

    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    assert app_session.scalars(sa.select(AlternateCopy)).one().published_at == PUBLISHED


def test_the_work_row_is_discharged_and_no_successor_is_enqueued(app_session: Session) -> None:
    """A collapsed article owes nothing: it no longer exists."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(app_session, other, guid="dupe", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()

    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    assert app_session.scalars(sa.select(PipelineWork)).all() == []


def test_collapsing_into_a_clustered_representative_reprojects_its_coverage(
    app_session: Session,
) -> None:
    """RFC §6.3: the duplicate never enters the article chain and the representative has no
    stage left to complete, so neither projection trigger fires. Without the re-projection the
    second publisher is silently absent from "also reported by" while being correctly present
    in ``alternate_copy`` — the write model right and the read model wrong.
    """
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(
        app_session, origin, guid="rep", state=PipelineState.CLUSTERED, published_at=PUBLISHED
    )
    cluster = make_cluster(app_session)
    make_member(app_session, cluster, representative)
    duplicate = make_article(
        app_session,
        other,
        guid="dupe",
        url="https://opendemocracy.test/dupe",
        state=PipelineState.ACQUIRED,
    )
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()

    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    covering = set(
        app_session.scalars(
            sa.select(ClusterProjectionSource.source_name).where(
                ClusterProjectionSource.cluster_id == cluster.cluster_id
            )
        )
    )
    assert covering == {"Global Voices", "openDemocracy"}


def test_an_unclustered_representative_is_not_projected_and_does_not_raise(
    app_session: Session,
) -> None:
    """The ordinary case — dedup runs before clustering, so most representatives have no
    cluster yet. ``project_article_cluster`` refuses an unclustered article by design."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(app_session, other, guid="dupe", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()

    work_queue.collapse(app_session, work, into=representative.article_id)
    app_session.commit()

    assert app_session.scalars(sa.select(ClusterProjectionSource)).all() == []


def test_a_record_holding_copies_is_refused(app_session: Session) -> None:
    """⚠️ ``alternate_copy`` cascades from ``canonical_record``, so collapsing a record that is
    already a representative would delete its notes with it — destroying the provenance
    collapse exists to keep, with nothing raised."""
    origin = make_source(app_session, "Global Voices")
    middle = make_source(app_session, "openDemocracy")
    third = make_source(app_session, "Waging Nonviolence")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    former = make_article(app_session, middle, guid="former", state=PipelineState.ACQUIRED)
    make_alternate_copy(app_session, former, third)
    work = make_work(app_session, stage=Stage.DEDUP, article=former)
    app_session.commit()

    with pytest.raises(ValueError, match="already holds collapsed copies"):
        work_queue.collapse(app_session, work, into=representative.article_id)


def test_collapsing_into_itself_is_refused(app_session: Session) -> None:
    """A match query that returned its own row would otherwise delete the record it just
    attached a note to."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="self", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=article)
    app_session.commit()

    with pytest.raises(ValueError, match="cannot collapse into itself"):
        work_queue.collapse(app_session, work, into=article.article_id)


def test_a_missing_representative_is_refused(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="dupe", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=article)
    app_session.commit()

    with pytest.raises(ValueError, match="is gone"):
        work_queue.collapse(app_session, work, into=article.article_id + 10_000)


def test_a_cluster_subject_is_refused(app_session: Session) -> None:
    """``terminal_reason`` and a collapse both live on an article; the symmetry with
    ``terminate`` is deliberate."""
    cluster = make_cluster(app_session)
    work = make_work(app_session, stage=Stage.SUMMARIZE, cluster=cluster)
    app_session.commit()

    with pytest.raises(ValueError, match="only an article can be collapsed"):
        work_queue.collapse(app_session, work, into=1)


def test_a_row_another_worker_discharged_is_refused(app_session: Session) -> None:
    """The ownership check runs before anything is destroyed."""
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(app_session, other, guid="dupe", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()

    app_session.execute(sa.delete(PipelineWork).where(PipelineWork.work_id == work.work_id))
    app_session.commit()

    with pytest.raises(work_queue.StaleWork):
        work_queue.collapse(app_session, work, into=representative.article_id)


def test_a_representative_deleted_mid_collapse_is_refused_cleanly(
    app_session: Session, app_migrated: sa.Engine
) -> None:
    """⚠️ What the ``FOR UPDATE`` on the representative buys, made deterministic.

    A writer takes the row's lock first, so the interleaving is not a race: the collapse blocks
    where it reads the representative, the writer deletes it and commits, and the collapse then
    finds no row and says so. Without the lock the read sails past — the row is merely locked,
    not yet gone — and the collapse instead blocks later, inside the ``AlternateCopy`` insert,
    on the foreign key's own weaker lock, and fails with an integrity error about a constraint
    rather than a sentence about a missing article. A foreign key's lock is not this function's
    lock, and this is the difference it makes.
    """
    origin = make_source(app_session, "Global Voices")
    other = make_source(app_session, "openDemocracy")
    representative = make_article(app_session, origin, guid="rep", state=PipelineState.DEDUPED)
    duplicate = make_article(app_session, other, guid="dupe", state=PipelineState.ACQUIRED)
    work = make_work(app_session, stage=Stage.DEDUP, article=duplicate)
    app_session.commit()
    representative_id = representative.article_id

    holding = threading.Event()

    def delete_it() -> None:
        with Session(app_migrated) as writer:
            writer.execute(
                sa.select(CanonicalRecord.article_id)
                .where(CanonicalRecord.article_id == representative_id)
                .with_for_update()
            ).one()
            holding.set()
            # Long enough for the collapse to reach its own read and block there.
            time.sleep(0.3)
            writer.execute(
                sa.delete(CanonicalRecord).where(CanonicalRecord.article_id == representative_id)
            )
            writer.commit()

    writer_thread = threading.Thread(target=delete_it)
    writer_thread.start()
    try:
        assert holding.wait(timeout=5), "the writer never took the lock"
        with pytest.raises(ValueError, match="is gone"):
            work_queue.collapse(app_session, work, into=representative_id)
    finally:
        writer_thread.join(timeout=5)
        app_session.rollback()
