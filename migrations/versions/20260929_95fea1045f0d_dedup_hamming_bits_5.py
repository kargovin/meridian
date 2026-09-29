"""dedup_hamming_bits 3 -> 5

Revision ID: 95fea1045f0d
Revises: 5b8e1d07a4c2
Create Date: 2026-09-29 10:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95fea1045f0d"
down_revision: str | Sequence[str] | None = "5b8e1d07a4c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Move the match threshold from its first default to 5.

    Only a row still holding the old default moves: a value set through the admin surface is
    somebody's decision and stays. ``test_every_seeded_value_matches_its_declared_default``
    reads this statement as well as the seeding one.
    """
    op.execute(
        """
        UPDATE runtime_config SET value = '5'
        WHERE key = 'dedup_hamming_bits' AND value = '3'
        """
    )


def downgrade() -> None:
    """Put the old default back, under the same condition."""
    op.execute(
        """
        UPDATE runtime_config SET value = '3'
        WHERE key = 'dedup_hamming_bits' AND value = '5'
        """
    )
