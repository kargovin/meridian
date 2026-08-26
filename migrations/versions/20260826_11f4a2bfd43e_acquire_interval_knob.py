"""acquire interval knob

Revision ID: 11f4a2bfd43e
Revises: 18b6d260b90e
Create Date: 2026-08-26 13:23:34.973213

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "11f4a2bfd43e"
down_revision: str | Sequence[str] | None = "18b6d260b90e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Seed the acquire cadence.

    Seeded rather than created on first write, like every knob: the row always exists, so a
    write is an update against a version token and there is no second path that creates one.

    ⚠️ 30 is written here as a literal and declared again in ``runtime_config.py``, because a
    migration must not import application code. ``test_every_seeded_value_matches_its_declared_default``
    is what stops the two drifting.
    """
    op.execute(
        """
        INSERT INTO runtime_config (key, value)
        VALUES ('acquire_interval_seconds', '30')
        ON CONFLICT (key) DO NOTHING
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM runtime_config WHERE key = 'acquire_interval_seconds'")
