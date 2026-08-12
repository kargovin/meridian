"""The database CHECKs and the libs/contract enums must hold the same values.

``alembic check`` cannot do this. It notices a CHECK constraint appearing or disappearing by
name, never a change to its expression — so adding or removing an enum member, which changes
nothing but the expression, produces no diff and no error. The column would then accept the
new value in Python and reject it in the database.
"""

import re
from enum import StrEnum

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from meridian.db.models import Base
from meridian.db.types import StrEnumType

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
    migrated: sa.Engine, table: str, column: str, enum_cls: type[StrEnum]
) -> None:
    name = f"ck_{table}_{column}"
    with migrated.connect() as conn:
        definition = conn.execute(
            sa.text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint"
                " WHERE conname = :name AND connamespace = 'public'::regnamespace"
            ),
            {"name": name},
        ).scalar()

    assert definition is not None, f"{table}.{column} is a StrEnumType with no CHECK {name}"
    assert set(_LITERAL.findall(definition)) == {member.value for member in enum_cls}


def test_the_database_rejects_an_unknown_value(session: Session) -> None:
    """The last line of defence, and the only one that survives raw SQL."""
    with pytest.raises(sa.exc.IntegrityError):
        session.execute(sa.text("INSERT INTO cluster (window_status) VALUES ('bogus')"))
        session.commit()
