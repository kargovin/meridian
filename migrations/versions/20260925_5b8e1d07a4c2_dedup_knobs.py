"""dedup knobs

Revision ID: 5b8e1d07a4c2
Revises: d3cdd889525c
Create Date: 2026-09-25 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b8e1d07a4c2"
down_revision: str | Sequence[str] | None = "d3cdd889525c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Seed the dedup cadence and match threshold.

    ⚠️ Both values are written here as literals and declared again in ``runtime_config.py``,
    because a migration must not import application code.
    ``test_every_seeded_value_matches_its_declared_default`` is what stops the two drifting.
    One statement per knob: that test reads the seeds out of this file one ``VALUES`` at a time.
    """
    op.execute(
        """
        INSERT INTO runtime_config (key, value)
        VALUES ('dedup_interval_seconds', '30')
        ON CONFLICT (key) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO runtime_config (key, value)
        VALUES ('dedup_hamming_bits', '3')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM runtime_config WHERE key IN ('dedup_interval_seconds', 'dedup_hamming_bits')"
    )
