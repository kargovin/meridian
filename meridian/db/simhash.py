"""Storing an unsigned 64-bit SimHash in a signed ``bigint`` column."""

_UINT64_SIGN_BIT = 1 << 63
_UINT64_RANGE = 1 << 64


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
