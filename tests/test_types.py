"""Column helpers. No database — these are pure conversions."""

import pytest
from meridian_contract import PipelineState, Stage

from meridian.db.types import StrEnumType, simhash_from_db, simhash_to_db


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, 2**63, 2**63 + 1, 2**64 - 1])
def test_simhash_round_trips(value: int) -> None:
    assert simhash_from_db(simhash_to_db(value)) == value


@pytest.mark.parametrize("value", [0, 2**63 - 1, 2**63, 2**64 - 1])
def test_simhash_fits_a_signed_bigint(value: int) -> None:
    assert -(2**63) <= simhash_to_db(value) <= 2**63 - 1


@pytest.mark.parametrize("value", [-1, 2**64, 2**70])
def test_simhash_rejects_values_outside_uint64(value: int) -> None:
    with pytest.raises(ValueError):
        simhash_to_db(value)


def test_cache_key_distinguishes_enum_classes() -> None:
    """Two StrEnumTypes must not share a compiled-statement cache entry.

    SQLAlchemy builds the key from __init__'s parameter names read out of __dict__. If the
    enum class is stored under a different or private name it is silently absent from the
    key, and a cached statement binds values through the wrong enum.
    """
    assert StrEnumType(Stage)._static_cache_key != StrEnumType(PipelineState)._static_cache_key


def test_bind_accepts_members_and_their_values() -> None:
    column = StrEnumType(PipelineState)
    assert column.process_bind_param(PipelineState.ACQUIRED, None) == "acquired"
    assert column.process_bind_param("acquired", None) == "acquired"
    assert column.process_bind_param(None, None) is None


def test_bind_rejects_an_unknown_value() -> None:
    """The first line of defence: a typo raises where it was written, not at commit."""
    with pytest.raises(ValueError):
        StrEnumType(PipelineState).process_bind_param("acquird", None)


def test_results_come_back_as_enum_members() -> None:
    """Identity, not just equality — `is` comparisons on enums are idiomatic."""
    column = StrEnumType(PipelineState)
    assert column.process_result_value("acquired", None) is PipelineState.ACQUIRED
    assert column.process_result_value(None, None) is None
