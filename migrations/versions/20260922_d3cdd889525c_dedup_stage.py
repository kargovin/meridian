"""dedup stage

Revision ID: d3cdd889525c
Revises: c3e1fa9f2e61
Create Date: 2026-09-22 23:23:42.217563

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3cdd889525c"
down_revision: str | Sequence[str] | None = "c3e1fa9f2e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bare names: the metadata naming convention (ck_%(table_name)s_%(constraint_name)s) builds
# the prefix, and passing the built name gets it prefixed a second time.
_STATE = "pipeline_state"
_STAGE = "stage"

_STATE_WITH_DEDUP = (
    "pipeline_state IN ('discovered', 'acquired', 'deduped', 'classified', 'clustered')"
)
_STATE_WITHOUT_DEDUP = "pipeline_state IN ('discovered', 'acquired', 'classified', 'clustered')"

_STAGE_WITH_DEDUP = "stage IN ('acquire', 'dedup', 'classify', 'cluster', 'summarize')"
_STAGE_WITHOUT_DEDUP = "stage IN ('acquire', 'classify', 'cluster', 'summarize')"


def upgrade() -> None:
    """Add the dedup stage, and the collapsed copy's own publication date.

    Dedup sits between acquire and classify, so an article that has a body has been checked
    for duplicates before anything downstream reads it.

    ⚠️ Hand-written, and it has to be: ``alembic check`` compares CHECK constraints by name
    only and cannot see a changed expression, so autogenerate emits nothing for a new enum
    member. ``tests/test_enum_constraints.py`` is what notices.

    The re-point at the end is what carries the articles already in flight. Every open row is
    matched on its article still sitting at ``acquired`` rather than on the stage alone —
    ``classify`` will hold rows that genuinely owe classification once that stage exists, and
    those must not be dragged backwards by a later run of this migration.

    ⚠️ Records already past ``acquired`` are not fingerprinted. Near-match candidates need a
    ``simhash``, so on a database holding such records they are invisible to near matching
    (exact matching on ``content_hash`` still sees them). Written for databases with none; one
    with a populated history needs a backfill first.
    """
    op.drop_constraint(_STATE, "canonical_record", type_="check")
    op.create_check_constraint(_STATE, "canonical_record", _STATE_WITH_DEDUP)

    op.drop_constraint(_STAGE, "pipeline_work", type_="check")
    op.create_check_constraint(_STAGE, "pipeline_work", _STAGE_WITH_DEDUP)

    # The duplicate's own publication date. The collapse deletes the record holding it, and a
    # copy published before its representative would otherwise take that date with it — while
    # the cluster headline is the earliest-published member.
    op.add_column(
        "alternate_copy",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        sa.text(
            "UPDATE pipeline_work SET stage = 'dedup'"
            " WHERE stage = 'classify'"
            "   AND dead_lettered_at IS NULL"
            "   AND article_id IN ("
            "     SELECT article_id FROM canonical_record"
            "      WHERE pipeline_state = 'acquired' AND terminal_reason IS NULL)"
        )
    )


def downgrade() -> None:
    """Remove the dedup stage.

    ⚠️ LOSSY, and it must be: ADD CONSTRAINT validates every existing row, so an article at
    ``deduped`` or a row at ``dedup`` has to be rewritten before the narrower CHECK goes back
    on. ``acquired`` / ``classify`` is where such a record stood before this stage existed, so
    it is re-offered to the stage that now follows acquire. The fingerprint written by dedup
    survives in ``simhash``; a collapse already performed is not undone, and the alternate copy
    it wrote keeps the provenance.

    ``published_at`` goes with the column.
    """
    op.execute(sa.text("UPDATE pipeline_work SET stage = 'classify' WHERE stage = 'dedup'"))
    op.execute(
        sa.text(
            "UPDATE canonical_record SET pipeline_state = 'acquired' WHERE pipeline_state = 'deduped'"
        )
    )

    op.drop_column("alternate_copy", "published_at")

    op.drop_constraint(_STAGE, "pipeline_work", type_="check")
    op.create_check_constraint(_STAGE, "pipeline_work", _STAGE_WITHOUT_DEDUP)

    op.drop_constraint(_STATE, "canonical_record", type_="check")
    op.create_check_constraint(_STATE, "canonical_record", _STATE_WITHOUT_DEDUP)
