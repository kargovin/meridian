"""Canned inference.

Deterministic, so a consumer can exercise every branch of the contract before the models
arrive: thin input is withheld, oversized input fails as an item, everything else succeeds.
The numbers are fixed values, not model output.
"""

from dataclasses import dataclass

from meridian_contract.api import Classification, ErrorCode, ErrorDetail

THIN_INPUT = 200
OVERSIZED = 20_000

_SUMMARY = "A canned summary standing in for model output."


@dataclass(frozen=True)
class StubSummary:
    summary: str = ""
    faithfulness_score: float = 0.0
    withheld: bool = False
    withhold_reason: str | None = None
    error: ErrorDetail | None = None


def summarize_documents(documents: list[dict[str, str]]) -> StubSummary:
    total = sum(len(document.get("text", "")) for document in documents)

    if total > OVERSIZED:
        return StubSummary(
            error=ErrorDetail(
                code=ErrorCode.ITEM_TOO_LARGE,
                message=f"{total} characters exceeds the {OVERSIZED} limit",
            )
        )
    if total < THIN_INPUT:
        return StubSummary(
            faithfulness_score=0.42, withheld=True, withhold_reason="below_faithfulness_bar"
        )
    return StubSummary(summary=_SUMMARY, faithfulness_score=0.97)


def classify_text(item_id: str, text: str) -> Classification:
    thin = len(text) < THIN_INPUT
    return Classification(
        id=item_id,
        topic="Other" if thin else "Technology",
        confidence=0.31 if thin else 0.94,
        fallback=thin,
    )
