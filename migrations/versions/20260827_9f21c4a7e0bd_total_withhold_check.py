"""make the projection's withhold check total

Revision ID: 9f21c4a7e0bd
Revises: c13e672c1a9b
Create Date: 2026-08-27 09:40:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9f21c4a7e0bd"
down_revision: str | Sequence[str] | None = "c13e672c1a9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare name: the metadata naming convention (ck_%(table_name)s_%(constraint_name)s)
# builds the prefix, and passing the built name gets it prefixed a second time.
_NAME = "summary_matches_withhold_reason"

_TOTAL = (
    "(withhold_reason IS NULL AND summary_text IS NULL)"
    " OR (withhold_reason IS NOT NULL AND withhold_reason = 'none')"
    " OR (withhold_reason IS NOT NULL AND withhold_reason <> 'none'"
    " AND summary_text IS NULL)"
)

_PARTIAL = (
    "(withhold_reason IS NULL AND summary_text IS NULL)"
    " OR (withhold_reason = 'none')"
    " OR (withhold_reason <> 'none' AND summary_text IS NULL)"
)


def upgrade() -> None:
    """Close the branch three-valued logic left open.

    A CHECK rejects a row only when it evaluates to FALSE. The previous expression compared
    against a NULL ``withhold_reason`` without testing for NULL first, so a row carrying
    summary text with no reason evaluated FALSE OR NULL OR FALSE — NULL, and accepted. That
    is the single combination the constraint exists to forbid.

    ⚠️ Hand-written, and it has to be: ``alembic check`` compares CHECK constraints by name
    only and cannot see a changed expression, so autogenerate emits nothing for this.
    """
    op.drop_constraint(_NAME, "cluster_projection", type_="check")
    op.create_check_constraint(_NAME, "cluster_projection", _TOTAL)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(_NAME, "cluster_projection", type_="check")
    op.create_check_constraint(_NAME, "cluster_projection", _PARTIAL)
