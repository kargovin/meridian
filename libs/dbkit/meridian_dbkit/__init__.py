"""Column types and constraint conventions shared by both deployables' schemas.

There is deliberately no declarative base here. Each deployable declares its own, so that
its Alembic tree sees only its own tables; a shared MetaData would collect both deployables'
tables into one object and each tree would propose creating the other's. The banned-import
rule in this package's ruff.toml is what keeps that unwritable.
"""

from enum import StrEnum
from typing import Any, TypeVar

import sqlalchemy as sa

#: Applied to a deployable's MetaData. Without it Alembic autogenerates server-assigned
#: names for CHECK and UNIQUE constraints, and a later migration cannot drop by name what it
#: did not name.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: sha256 hex digest.
Sha256 = sa.String(64)

#: Timestamps are always timezone-aware.
TZDateTime = sa.DateTime(timezone=True)

E = TypeVar("E", bound=StrEnum)


class StrEnumType(sa.types.TypeDecorator[E]):
    """A ``varchar`` column that round-trips a ``StrEnum`` member.

    Not ``sa.Enum``: with ``native_enum=False`` that type creates its CHECK constraint
    inside the type, where autogenerate cannot see it in the metadata and proposes dropping
    it on every run. Declare the constraint separately with :func:`enum_check`.
    """

    impl = sa.String
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        # The attribute must be named for the constructor parameter and must not start with
        # an underscore: SQLAlchemy builds the statement-cache key from __init__'s parameter
        # names looked up in __dict__, skipping private ones. Rename it and every
        # StrEnumType shares one cache entry, so a cached statement binds the wrong enum.
        self.enum_cls = enum_cls
        super().__init__(length=max(len(member.value) for member in enum_cls))

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        return None if value is None else self.enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        return None if value is None else self.enum_cls(value)


def enum_check(column: str, enum_cls: type[StrEnum], name: str | None = None) -> sa.CheckConstraint:
    """The CHECK constraint restricting ``column`` to ``enum_cls``'s values.

    Rendered ``ck_<table>_<name>``, defaulting to the column name.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name or column)


__all__ = [
    "NAMING_CONVENTION",
    "Sha256",
    "StrEnumType",
    "TZDateTime",
    "enum_check",
]
