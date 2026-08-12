"""Column helpers shared across the models."""

from enum import StrEnum
from typing import Any, TypeVar

import sqlalchemy as sa

#: sha256 hex digest.
Sha256 = sa.String(64)

#: Timestamps are always timezone-aware.
TZDateTime = sa.DateTime(timezone=True)

_UINT64_SIGN_BIT = 1 << 63
_UINT64_RANGE = 1 << 64

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
        self._enum_cls = enum_cls
        super().__init__(length=max(len(member.value) for member in enum_cls))

    def process_bind_param(self, value: E | str | None, dialect: Any) -> str | None:
        return None if value is None else self._enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> E | None:
        return None if value is None else self._enum_cls(value)


def enum_check(column: str, enum_cls: type[StrEnum], name: str | None = None) -> sa.CheckConstraint:
    """The CHECK constraint restricting ``column`` to ``enum_cls``'s values.

    Rendered ``ck_<table>_<name>``, defaulting to the column name.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name or column)


def simhash_to_db(value: int) -> int:
    """Map an unsigned 64-bit SimHash onto the signed ``bigint`` the column stores.

    Values above 2^63 do not fit a signed column; the bit pattern is preserved, so equality
    and popcount are unaffected. Numeric ordering of stored values is meaningless — never
    sort or range-query on this column.
    """
    if not 0 <= value < _UINT64_RANGE:
        raise ValueError(f"simhash out of uint64 range: {value}")
    return value - _UINT64_RANGE if value >= _UINT64_SIGN_BIT else value


def simhash_from_db(value: int) -> int:
    """Inverse of :func:`simhash_to_db`."""
    return value + _UINT64_RANGE if value < 0 else value
