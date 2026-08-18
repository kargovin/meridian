"""Canned inference.

Deterministic, so a consumer can exercise every branch of the contract before the models
arrive: thin input is withheld, oversized input fails as an item, everything else succeeds.
The numbers and the prose are fixed values, not model output.
"""

from dataclasses import dataclass, field

from meridian_contract.api import Classification, ErrorCode, ErrorDetail

THIN_INPUT = 200
OVERSIZED = 20_000

_SENTENCES = [
    "A canned summary standing in for model output.",
    "The shapes, limits and error model are real; this prose is not.",
    "Real summarization replaces this without changing the contract.",
]


@dataclass(frozen=True)
class StubSummary:
    summary: str = ""
    faithfulness_score: float = 0.0
    withheld: bool = False
    withhold_reason: str | None = None
    provenance: list[str] = field(default_factory=list)
    error: ErrorDetail | None = None


def summarize_documents(
    documents: list[dict[str, str]], max_sentences: int | None = None
) -> StubSummary:
    """``max_sentences`` is honoured rather than accepted and ignored."""
    if not documents:
        return StubSummary(
            error=ErrorDetail(code=ErrorCode.INVALID_REQUEST, message="no documents supplied")
        )

    total = sum(len(document.get("text", "")) for document in documents)
    provenance = [url for document in documents if (url := document.get("url"))]

    if total > OVERSIZED:
        return StubSummary(
            error=ErrorDetail(
                code=ErrorCode.ITEM_TOO_LARGE,
                message=f"{total} characters exceeds the {OVERSIZED} limit",
            )
        )
    if total < THIN_INPUT:
        return StubSummary(
            faithfulness_score=0.42,
            withheld=True,
            withhold_reason="below_faithfulness_bar",
            provenance=provenance,
        )

    wanted = (
        len(_SENTENCES) if max_sentences is None else max(1, min(max_sentences, len(_SENTENCES)))
    )
    return StubSummary(
        summary=" ".join(_SENTENCES[:wanted]),
        faithfulness_score=0.97,
        provenance=provenance,
    )


@dataclass(frozen=True)
class StubClassification:
    result: Classification | None = None
    error: ErrorDetail | None = None


def classify_text(item_id: str, text: str) -> StubClassification:
    """An oversized item fails as an item, so a batch can be partially successful."""
    if len(text) > OVERSIZED:
        return StubClassification(
            error=ErrorDetail(
                code=ErrorCode.ITEM_TOO_LARGE,
                message=f"{len(text)} characters exceeds the {OVERSIZED} limit",
                item_id=item_id,
            )
        )

    thin = len(text) < THIN_INPUT
    return StubClassification(
        result=Classification(
            id=item_id,
            topic="Other" if thin else "Technology",
            confidence=0.31 if thin else 0.94,
            fallback=thin,
        )
    )
