"""The queue is derived from the records, not a second source of truth."""

import pytest
import sqlalchemy as sa
from meridian_contract import PipelineState, Stage, TerminalReason
from sqlalchemy.orm import Session

from meridian.db.work_queue import expected_article_work, missing_article_work, open_article_work
from tests.factories import make_article, make_source, make_work

pytestmark = pytest.mark.postgres


@pytest.fixture
def populated(session: Session) -> dict[str, int]:
    """One article at each state, plus a terminal one, each with the work it owes."""
    source = make_source(session)
    ids = {}
    for label, state, stage in [
        ("discovered", PipelineState.DISCOVERED, Stage.ACQUIRE),
        ("acquired", PipelineState.ACQUIRED, Stage.CLASSIFY),
        ("classified", PipelineState.CLASSIFIED, Stage.CLUSTER),
    ]:
        article = make_article(session, source, guid=label, state=state)
        make_work(session, stage=stage, article=article)
        ids[label] = article.article_id

    ids["clustered"] = make_article(
        session, source, guid="clustered", state=PipelineState.CLUSTERED
    ).article_id
    ids["dropped"] = make_article(
        session,
        source,
        guid="dropped",
        state=PipelineState.DISCOVERED,
        terminal_reason=TerminalReason.DROPPED_LANGUAGE,
    ).article_id
    session.commit()
    return ids


def test_a_healthy_queue_is_missing_nothing(session: Session, populated: dict[str, int]) -> None:
    assert missing_article_work(session) == set()


def test_queue_survives_being_truncated(session: Session, populated: dict[str, int]) -> None:
    """AC4. Every article that owed work still owes it after the queue is thrown away."""
    before = open_article_work(session)
    assert before == expected_article_work(session)

    session.execute(sa.text("TRUNCATE pipeline_work RESTART IDENTITY"))
    session.commit()

    assert open_article_work(session) == set()
    assert expected_article_work(session) == before
    assert missing_article_work(session) == before


def test_finished_and_terminal_articles_owe_nothing(
    session: Session, populated: dict[str, int]
) -> None:
    owed = {article_id for article_id, _ in expected_article_work(session)}
    assert populated["clustered"] not in owed, "the article chain ends at clustered"
    assert populated["dropped"] not in owed, "a language drop is terminal, not retryable"


def test_expected_work_follows_the_chain(session: Session, populated: dict[str, int]) -> None:
    assert expected_article_work(session) == {
        (populated["discovered"], Stage.ACQUIRE),
        (populated["acquired"], Stage.CLASSIFY),
        (populated["classified"], Stage.CLUSTER),
    }


def test_a_dropped_enqueue_is_reported(session: Session, populated: dict[str, int]) -> None:
    """The failure the successor map exists to prevent: state advanced, nothing enqueued."""
    session.execute(
        sa.text("DELETE FROM pipeline_work WHERE article_id = :id"),
        {"id": populated["acquired"]},
    )
    session.commit()

    assert missing_article_work(session) == {(populated["acquired"], Stage.CLASSIFY)}
