"""SimHash column encoding. No database — these are pure conversions."""

import pytest

from meridian.db.simhash import simhash_from_db, simhash_to_db


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
