"""Retention, and the A1 property it keeps true.

``now`` is passed to ``sweep`` rather than read inside it, so a 24-hour rule is testable in
milliseconds.
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract.api import SourceDocument, SummarizeItem
from meridian_platform.db import SummarizeJob, SummarizeJobItem
from meridian_platform.jobs import RETENTION, enqueue, process_next
from meridian_platform.retention import sweep
from meridian_platform.stub import THIN_INPUT
from sqlalchemy.orm import Session

from meridian.db.models import Source

pytestmark = pytest.mark.postgres

GOOD = "x" * (THIN_INPUT + 100)


def item(item_id: str) -> SummarizeItem:
    return SummarizeItem(
        id=item_id,
        documents=[SourceDocument(source="outlet-a", title="t", text=GOOD, url="https://e/1")],
    )


def test_a_finished_job_keeps_no_input(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])
    process_next(platform_session)

    held = platform_session.scalars(
        sa.select(SummarizeJobItem).where(SummarizeJobItem.documents.isnot(None))
    ).all()

    assert held == []


def test_the_sweep_clears_input_left_on_a_terminal_job(platform_session: Session) -> None:
    """Belt and braces: the guarantee must hold even if a handler forgot."""
    enqueue(platform_session, "digest", [item("c1")])
    process_next(platform_session)
    platform_session.execute(sa.update(SummarizeJobItem).values(documents=[{"text": "leaked"}]))
    platform_session.commit()

    result = sweep(platform_session, dt.datetime.now(dt.UTC))

    assert result.inputs_discarded == 1


def test_a_job_is_deleted_once_its_window_passes(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])
    process_next(platform_session)

    result = sweep(platform_session, dt.datetime.now(dt.UTC) + RETENTION + dt.timedelta(minutes=1))

    assert result.jobs_deleted == 1
    assert result.overdue == 0
    assert platform_session.scalars(sa.select(SummarizeJob)).all() == []


def test_a_live_job_survives_the_sweep(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1")])

    result = sweep(platform_session, dt.datetime.now(dt.UTC))

    assert result.jobs_deleted == 0
    assert len(platform_session.scalars(sa.select(SummarizeJob)).all()) == 1


def test_deleting_a_job_takes_its_items_with_it(platform_session: Session) -> None:
    enqueue(platform_session, "digest", [item("c1"), item("c2")])
    process_next(platform_session)

    sweep(platform_session, dt.datetime.now(dt.UTC) + RETENTION + dt.timedelta(minutes=1))

    assert platform_session.scalars(sa.select(SummarizeJobItem)).all() == []


def test_truncating_the_platform_loses_nothing_of_the_application(
    platform_session: Session, app_session: Session
) -> None:
    """A1: the Platform holds no domain data, stated as something that can fail."""
    app_session.add(
        Source(
            name="Outlet A",
            home_url="https://a.example",
            discovery_method="rss",
            acquisition_tier="1_full_feed",
            rights_level="body_text",
            jurisdiction="GB",
            enabled=True,
            rate_limit_per_min=10,
        )
    )
    app_session.commit()
    enqueue(platform_session, "digest", [item("c1")])

    platform_session.execute(
        sa.text("TRUNCATE summarize_job, summarize_job_item RESTART IDENTITY CASCADE")
    )
    platform_session.commit()

    assert app_session.scalars(sa.select(Source)).all() != []
