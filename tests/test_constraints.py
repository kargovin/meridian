"""Constraints the pipeline's correctness rests on."""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract import Stage, WithholdReason
from sqlalchemy.orm import Session

from meridian.db.models import ClusterMember, PipelineWork, Summary
from tests.factories import (
    make_alternate_copy,
    make_article,
    make_cluster,
    make_source,
    make_work,
    sha256,
)

pytestmark = pytest.mark.postgres


def test_duplicate_content_hash_is_accepted(session: Session) -> None:
    """AC3. Exact duplicates must reach the dedup stage so it can collapse them.

    A unique constraint here could only error or DO NOTHING, and neither writes the
    AlternateCopy row that keeps provenance.
    """
    source = make_source(session)
    digest = sha256("identical body")
    make_article(session, source, guid="a", content_hash=digest)
    make_article(session, source, guid="b", content_hash=digest)
    session.commit()

    stored = session.execute(
        sa.select(sa.func.count()).select_from(sa.table("canonical_record"))
    ).scalar()
    assert stored == 2


def test_repolling_cannot_duplicate_a_representative(session: Session) -> None:
    source = make_source(session)
    make_article(session, source, guid="dup")
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_article(session, source, guid="dup", url="https://example.test/other")
        session.commit()


def test_repolling_cannot_duplicate_an_alternate_copy(session: Session) -> None:
    """Without this the provenance count climbs on every poll, on a timer, unprompted."""
    source = make_source(session)
    other = make_source(session, name="Other News")
    article = make_article(session, source, guid="wire")
    make_alternate_copy(session, article, other, guid="wire-bbc", url="https://other.test/wire")
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_alternate_copy(
            session, article, other, guid="wire-bbc", url="https://other.test/wire-again"
        )
        session.commit()


def test_alternate_copy_url_is_unique_even_without_a_guid(session: Session) -> None:
    """Tier-3 acquisition has no feed-native id, so UNIQUE(url) is the only cover."""
    source = make_source(session)
    other = make_source(session, name="Other News")
    article = make_article(session, source, guid="wire")
    make_alternate_copy(session, article, other, guid=None, url="https://other.test/x")
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_alternate_copy(session, article, other, guid=None, url="https://other.test/x")
        session.commit()


def test_an_article_belongs_to_at_most_one_cluster(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    first = make_cluster(session)
    second = make_cluster(session)
    session.add(ClusterMember(article_id=article.article_id, cluster_id=first.cluster_id))
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        session.add(ClusterMember(article_id=article.article_id, cluster_id=second.cluster_id))
        session.commit()


def test_withheld_summary_must_carry_a_reason(session: Session) -> None:
    cluster = make_cluster(session)
    with pytest.raises(sa.exc.IntegrityError):
        session.add(
            Summary(
                cluster_id=cluster.cluster_id,
                withheld=True,
                withhold_reason=WithholdReason.NONE,
            )
        )
        session.commit()


def test_work_has_exactly_one_subject(session: Session) -> None:
    source = make_source(session)
    article = make_article(session, source, guid="a")
    cluster = make_cluster(session)

    with pytest.raises(sa.exc.IntegrityError):
        session.add(PipelineWork(stage=Stage.CLASSIFY))
        session.commit()
    session.rollback()

    with pytest.raises(sa.exc.IntegrityError):
        session.add(
            PipelineWork(
                stage=Stage.CLASSIFY,
                article_id=article.article_id,
                cluster_id=cluster.cluster_id,
            )
        )
        session.commit()


def test_a_subject_has_at_most_one_open_work_row(session: Session) -> None:
    """The debounce: a second pending change collides here instead of enqueueing."""
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(session, stage=Stage.CLASSIFY, article=article)
    session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_work(session, stage=Stage.CLUSTER, article=article)
        session.commit()


def test_a_dead_lettered_subject_can_be_re_enqueued(session: Session) -> None:
    """Terminal-failure rows are retained, so the open-row index must exclude them."""
    source = make_source(session)
    article = make_article(session, source, guid="a")
    make_work(
        session,
        stage=Stage.CLASSIFY,
        article=article,
        dead_lettered_at=dt.datetime.now(dt.UTC),
    )
    session.commit()

    make_work(session, stage=Stage.CLASSIFY, article=article)
    session.commit()

    open_rows = session.execute(
        sa.select(sa.func.count())
        .select_from(PipelineWork)
        .where(PipelineWork.dead_lettered_at.is_(None))
    ).scalar()
    assert open_rows == 1
