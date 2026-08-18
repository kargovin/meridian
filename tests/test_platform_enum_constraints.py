"""The Platform's CHECKs and its enums must hold the same values.

``alembic check`` compares CHECK constraints by name only, so a changed expression — which is
all that adding an enum member is — produces no diff and no error.
"""

import re
from enum import StrEnum
from typing import get_args

import pytest
import sqlalchemy as sa
from meridian_contract.api import WireWithholdReason
from meridian_dbkit import StrEnumType
from meridian_platform.db import Base
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres

_LITERAL = re.compile(r"'([^']*)'")

ENUM_COLUMNS: list[tuple[str, str, type[StrEnum]]] = [
    (table.name, column.name, column.type.enum_cls)
    for table in Base.metadata.tables.values()
    for column in table.columns
    if isinstance(column.type, StrEnumType)
]


def test_there_are_enum_columns_to_check() -> None:
    """Guards the parametrized test below from passing vacuously on an empty list."""
    assert ENUM_COLUMNS


@pytest.mark.parametrize(
    ("table", "column", "enum_cls"),
    ENUM_COLUMNS,
    ids=[f"{table}.{column}" for table, column, _ in ENUM_COLUMNS],
)
def test_check_holds_exactly_the_enum_values(
    platform_migrated: sa.Engine, table: str, column: str, enum_cls: type[StrEnum]
) -> None:
    name = f"ck_{table}_{column}"
    with platform_migrated.connect() as conn:
        definition = conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = :name AND connamespace = 'public'::regnamespace"
            ),
            {"name": name},
        ).scalar()

    assert definition is not None, f"{table}.{column} is a StrEnumType with no CHECK {name}"
    assert set(_LITERAL.findall(definition)) == {member.value for member in enum_cls}


def test_the_withhold_reason_check_matches_the_wire_vocabulary(
    platform_migrated: sa.Engine,
) -> None:
    """The constraint is built from the wire Literal, so nothing may drift between them.

    Not covered by the parametrized test above: this column is a plain String with a
    hand-built CHECK rather than a StrEnumType. And not covered by ``alembic check``, which
    compares CHECK constraints by name only — adding a wire reason changes the expression
    and nothing else, so without this the column would reject a value the contract permits.
    """
    with platform_migrated.connect() as conn:
        definition = conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = 'ck_summarize_job_item_withhold_reason'"
                " AND connamespace = 'public'::regnamespace"
            )
        ).scalar()

    assert definition is not None, "the withhold_reason CHECK is missing"
    assert set(_LITERAL.findall(definition)) == set(get_args(WireWithholdReason))


def test_the_withhold_reason_check_admits_only_wire_values(platform_session: Session) -> None:
    """The column must not accept a reason this service can never produce."""
    platform_session.execute(
        sa.text(
            "INSERT INTO summarize_job (public_id, consumer, status, expires_at)"
            " VALUES (gen_random_uuid(), 'digest', 'queued', now())"
        )
    )
    platform_session.commit()

    with pytest.raises(sa.exc.IntegrityError):
        platform_session.execute(
            sa.text(
                "INSERT INTO summarize_job_item (job_id, item_id, withhold_reason)"
                " SELECT id, 'c1', 'rights_excluded' FROM summarize_job LIMIT 1"
            )
        )
        platform_session.commit()
