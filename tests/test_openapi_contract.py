"""The published document is generated from the models, never hand-written."""

import json
from typing import Any, get_args

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


def _document() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(DOCUMENT.read_text())
    return document


def _v1_operations() -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{method.upper()} {path}", operation)
        for path, methods in _document()["paths"].items()
        for method, operation in methods.items()
    ]


def test_the_document_declares_one_error_shape() -> None:
    """Regenerating after a change keeps the freeze test green, so it cannot see this.

    FastAPI documents a 422 with its own ``{"detail": [...]}`` body on any route that
    validates input. The locked model is ``{"error": {...}}``; publishing both puts a second
    error shape in a contract that says there is one.
    """
    offenders = [name for name, op in _v1_operations() if "422" in op.get("responses", {})]

    assert offenders == []
    assert "HTTPValidationError" not in json.dumps(_document())


def test_every_operation_requires_a_credential() -> None:
    """A client generated from a contract with no security scheme sends none."""
    document = _document()
    schemes = document.get("components", {}).get("securitySchemes", {})

    assert schemes, "no securityScheme: the contract does not say callers must authenticate"

    unguarded = [name for name, op in _v1_operations() if not op.get("security")]
    assert unguarded == []
