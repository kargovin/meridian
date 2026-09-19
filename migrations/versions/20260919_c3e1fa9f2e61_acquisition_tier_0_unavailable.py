"""acquisition tier 0_unavailable

Revision ID: c3e1fa9f2e61
Revises: 9f21c4a7e0bd
Create Date: 2026-09-19 13:48:50.936818

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3e1fa9f2e61"
down_revision: str | Sequence[str] | None = "9f21c4a7e0bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare name: the metadata naming convention (ck_%(table_name)s_%(constraint_name)s)
# builds the prefix, and passing the built name gets it prefixed a second time.
_NAME = "acquisition_tier"

_WITH_UNAVAILABLE = (
    "acquisition_tier IN ('0_unavailable', '1_full_feed', '2_publisher_api', '3_extraction')"
)
_WITHOUT_UNAVAILABLE = "acquisition_tier IN ('1_full_feed', '2_publisher_api', '3_extraction')"


def upgrade() -> None:
    """Add ``0_unavailable`` to the feed's acquisition-tier vocabulary.

    A feed whose article pages yield no body by any route we run (a login gate, for one) had
    no honest tier: ``3_extraction`` fetches and stores whatever preview the gate shows, and
    the two feed-level tiers are false. Its records carry a headline and lede only.

    ⚠️ Hand-written, and it has to be: ``alembic check`` compares CHECK constraints by name
    only and cannot see a changed expression, so autogenerate emits nothing for a new enum
    member. ``tests/test_enum_constraints.py`` is what notices.
    """
    op.drop_constraint(_NAME, "feed", type_="check")
    op.create_check_constraint(_NAME, "feed", _WITH_UNAVAILABLE)


def downgrade() -> None:
    """Restore the three-valued CHECK.

    ⚠️ LOSSY: a feed at ``0_unavailable`` is rewritten to ``3_extraction`` first, because
    ADD CONSTRAINT validates every existing row and would refuse otherwise — and the test
    fixtures round-trip through ``downgrade base`` on a database the previous run left populated.
    ``3_extraction`` is what such a feed was recorded as before the member existed; downgrading
    puts it back to fetching the page.
    """
    op.execute(
        sa.text(
            "UPDATE feed SET acquisition_tier = '3_extraction'"
            " WHERE acquisition_tier = '0_unavailable'"
        )
    )
    op.drop_constraint(_NAME, "feed", type_="check")
    op.create_check_constraint(_NAME, "feed", _WITHOUT_UNAVAILABLE)
