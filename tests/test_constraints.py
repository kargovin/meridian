"""Constraints the pipeline's correctness rests on."""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract import Stage, WithholdReason
from sqlalchemy.exc import IntegrityError
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


def test_duplicate_content_hash_is_accepted(app_session: Session) -> None:
    """AC3. Exact duplicates must reach the dedup stage so it can collapse them.

    A unique constraint here could only error or DO NOTHING, and neither writes the
    AlternateCopy row that keeps provenance.
    """
    source = make_source(app_session)
    digest = sha256("identical body")
    make_article(app_session, source, guid="a", content_hash=digest)
    make_article(app_session, source, guid="b", content_hash=digest)
    app_session.commit()

    stored = app_session.execute(
        sa.select(sa.func.count()).select_from(sa.table("canonical_record"))
    ).scalar()
    assert stored == 2


def test_repolling_cannot_duplicate_a_representative(app_session: Session) -> None:
    source = make_source(app_session)
    make_article(app_session, source, guid="dup")
    app_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_article(app_session, source, guid="dup", url="https://example.test/other")
        app_session.commit()


def test_repolling_cannot_duplicate_an_alternate_copy(app_session: Session) -> None:
    """Without this the provenance count climbs on every poll, on a timer, unprompted."""
    source = make_source(app_session)
    other = make_source(app_session, name="Other News")
    article = make_article(app_session, source, guid="wire")
    make_alternate_copy(app_session, article, other, guid="wire-bbc", url="https://other.test/wire")
    app_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_alternate_copy(
            app_session, article, other, guid="wire-bbc", url="https://other.test/wire-again"
        )
        app_session.commit()


def test_alternate_copy_url_is_unique_even_without_a_guid(app_session: Session) -> None:
    """Tier-3 acquisition has no feed-native id, so UNIQUE(url) is the only cover."""
    source = make_source(app_session)
    other = make_source(app_session, name="Other News")
    article = make_article(app_session, source, guid="wire")
    make_alternate_copy(app_session, article, other, guid=None, url="https://other.test/x")
    app_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_alternate_copy(app_session, article, other, guid=None, url="https://other.test/x")
        app_session.commit()


def test_an_article_belongs_to_at_most_one_cluster(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    first = make_cluster(app_session)
    second = make_cluster(app_session)
    app_session.add(ClusterMember(article_id=article.article_id, cluster_id=first.cluster_id))
    app_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        app_session.add(ClusterMember(article_id=article.article_id, cluster_id=second.cluster_id))
        app_session.commit()


def test_withheld_summary_must_carry_a_reason(app_session: Session) -> None:
    cluster = make_cluster(app_session)
    with pytest.raises(sa.exc.IntegrityError):
        app_session.add(
            Summary(
                cluster_id=cluster.cluster_id,
                withheld=True,
                withhold_reason=WithholdReason.NONE,
            )
        )
        app_session.commit()


def test_a_withheld_summary_cannot_keep_its_text(app_session: Session) -> None:
    """Otherwise a rights-excluded summary still carries the body it was excluded for."""
    cluster = make_cluster(app_session)
    with pytest.raises(sa.exc.IntegrityError):
        app_session.add(
            Summary(
                cluster_id=cluster.cluster_id,
                withheld=True,
                withhold_reason=WithholdReason.RIGHTS_EXCLUDED,
                text="the full article body",
            )
        )
        app_session.commit()


def test_work_has_exactly_one_subject(app_session: Session) -> None:
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    cluster = make_cluster(app_session)

    with pytest.raises(sa.exc.IntegrityError):
        app_session.add(PipelineWork(stage=Stage.CLASSIFY))
        app_session.commit()
    app_session.rollback()

    with pytest.raises(sa.exc.IntegrityError):
        app_session.add(
            PipelineWork(
                stage=Stage.CLASSIFY,
                article_id=article.article_id,
                cluster_id=cluster.cluster_id,
            )
        )
        app_session.commit()


def test_a_subject_has_at_most_one_open_work_row(app_session: Session) -> None:
    """The debounce: a second pending change collides here instead of enqueueing."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(app_session, stage=Stage.CLASSIFY, article=article)
    app_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        make_work(app_session, stage=Stage.CLUSTER, article=article)
        app_session.commit()


def test_a_dead_lettered_subject_can_be_re_enqueued(app_session: Session) -> None:
    """Terminal-failure rows are retained, so the open-row index must exclude them."""
    source = make_source(app_session)
    article = make_article(app_session, source, guid="a")
    make_work(
        app_session,
        stage=Stage.CLASSIFY,
        article=article,
        dead_lettered_at=dt.datetime.now(dt.UTC),
    )
    app_session.commit()

    make_work(app_session, stage=Stage.CLASSIFY, article=article)
    app_session.commit()

    open_rows = app_session.execute(
        sa.select(sa.func.count())
        .select_from(PipelineWork)
        .where(PipelineWork.dead_lettered_at.is_(None))
    ).scalar()
    assert open_rows == 1


def test_the_rate_limit_must_be_positive(app_session: Session) -> None:
    """Pins the CHECK's expression, which no schema tool can see.

    ``alembic check`` compares check constraints by name only — altering this one to ``>= 0``
    in the database leaves ``compare_metadata`` returning no diff at all. FR-I3 politeness has
    no meaning at zero, and the pipeline's obvious use of it has no defined behaviour there.
    """
    with pytest.raises(IntegrityError):
        make_source(app_session, name="Impolite", rate_limit_per_min=0)
