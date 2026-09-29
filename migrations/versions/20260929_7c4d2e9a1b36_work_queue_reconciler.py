"""work queue reconciler

Revision ID: 7c4d2e9a1b36
Revises: 95fea1045f0d
Create Date: 2026-09-29 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4d2e9a1b36"
down_revision: str | Sequence[str] | None = "95fea1045f0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """When each work row was enqueued, and the reconciler's cadence.

    ``enqueued_at`` is what the age of a stage's oldest open row is measured from. Neither
    existing timestamp can stand in: ``next_attempt_at`` moves on every release and
    ``claimed_at`` on every heartbeat, so a row released forever looks young by both.

    Existing rows get the earliest timestamp they carry. For a row never released that is its
    insert time exactly (``next_attempt_at`` defaults to it); for one released since, it is the
    best bound left, and later than the truth.

    ⚠️ The cadence is written here as a literal and declared again in ``runtime_config.py``,
    because a migration must not import application code.
    ``test_every_seeded_value_matches_its_declared_default`` is what stops the two drifting.
    """
    op.add_column(
        "pipeline_work",
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE pipeline_work SET enqueued_at = LEAST(next_attempt_at, claimed_at, now())")
    op.alter_column("pipeline_work", "enqueued_at", nullable=False, server_default=sa.text("now()"))
    op.execute(
        """
        INSERT INTO runtime_config (key, value)
        VALUES ('reconcile_interval_seconds', '300')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM runtime_config WHERE key = 'reconcile_interval_seconds'")
    op.drop_column("pipeline_work", "enqueued_at")
