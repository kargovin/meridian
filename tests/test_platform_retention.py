"""Retention, and the A1 property it keeps true.

``now`` is passed to ``sweep`` rather than read inside it, so a 24-hour rule is testable in
milliseconds.
"""

import datetime as dt

import pytest
import sqlalchemy as sa
from meridian_contract.api import SourceDocument, SummarizeItem
from meridian_platform.db import Base as PlatformBase
from meridian_platform.db import SummarizeJob, SummarizeJobItem
from meridian_platform.jobs import RETENTION, enqueue, process_next
from meridian_platform.retention import sweep
from meridian_platform.stub import THIN_INPUT
from sqlalchemy.orm import Session

from meridian.db.models import Base as AppBase
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


def test_truncating_the_platform_leaves_the_application_intact(
    platform_session: Session, app_session: Session
) -> None:
    """A1, as far as a test can carry it.

    The cross-database half is structural — the two live in different databases, so a
    TRUNCATE here could not reach the application's tables however this were written; that
    separation is what ``test_platform_isolation`` covers. What this does check is that the
    truncate list is derived from the Platform's own metadata rather than hand-maintained,
    so a Platform table added later is emptied by it and noticed here rather than quietly
    surviving, and that the application's row counts are unchanged across every one of its
    tables rather than a chosen few.
    """
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

    def app_counts() -> dict[str, int]:
        return {
            name: app_session.scalar(sa.select(sa.func.count()).select_from(table)) or 0
            for name, table in AppBase.metadata.tables.items()
        }

    before = app_counts()
    assert before["source"] == 1, "the fixture wrote nothing, so nothing could be lost"

    platform_tables = ", ".join(f'"{name}"' for name in PlatformBase.metadata.tables)
    platform_session.execute(sa.text(f"TRUNCATE {platform_tables} RESTART IDENTITY CASCADE"))
    platform_session.commit()

    assert app_counts() == before
    for table in PlatformBase.metadata.tables.values():
        assert platform_session.scalar(sa.select(sa.func.count()).select_from(table)) == 0


def test_overdue_counts_what_the_sweep_could_not_reach(platform_session: Session) -> None:
    """The signal must be able to be non-zero, or it reports success by construction."""
    for n in range(3):
        enqueue(platform_session, "digest", [item(f"c{n}")])
        process_next(platform_session)
    past = dt.datetime.now(dt.UTC) + RETENTION + dt.timedelta(minutes=1)

    result = sweep(platform_session, past, batch=1)

    assert result.jobs_deleted == 1
    assert result.overdue == 2
