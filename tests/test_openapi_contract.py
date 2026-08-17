"""The published document is generated from the models, never hand-written."""

from typing import get_args

from meridian_contract import WithholdReason
from meridian_contract.api import WireWithholdReason
from meridian_platform.openapi import DOCUMENT, render


def _published_withhold_reasons() -> set[str]:
    """Which members of the shared enum appear anywhere in the document.

    Scans the whole file rather than the field: typing the field with the enum emits a
    ``$ref`` to a shared schema, so the values move out of the property and a check scoped
    to it reports an empty set instead of the four values actually published.
    """
    document = DOCUMENT.read_text()
    return {member.value for member in WithholdReason if f'"{member.value}"' in document}


def test_the_committed_document_matches_the_models() -> None:
    assert DOCUMENT.read_text() == render(), (
        "platform/openapi.json is stale — run `uv run python -m meridian_platform.openapi`"
    )


def test_every_wire_withhold_reason_is_storable() -> None:
    storable = {member.value for member in WithholdReason}

    assert set(get_args(WireWithholdReason)) <= storable


def test_the_storage_sentinel_never_reaches_the_wire() -> None:
    assert WithholdReason.NONE.value not in get_args(WireWithholdReason)


def test_the_document_publishes_exactly_the_wire_reasons() -> None:
    """Typing the field with the shared enum publishes every member instead."""
    assert _published_withhold_reasons() == set(get_args(WireWithholdReason))
