"""``POST /v1/summarize`` (RFC §8.1, PRD §8.2)."""

from typing import Literal

from pydantic import BaseModel, Field

from meridian_contract.api.errors import ErrorDetail

#: Sync batch ceiling (2.3 / J4). Above this the call is answered 202 + job id.
SUMMARIZE_SYNC_MAX_BATCH = 2

#: The only withhold reason this service can produce. NOT ``WithholdReason``: that enum
#: carries the storage sentinel ``none`` and two app-side reasons, and typing the field with
#: it publishes all four into the document as legal values.
WireWithholdReason = Literal["below_faithfulness_bar"]


class SourceDocument(BaseModel):
    source: str
    title: str
    text: str
    url: str


class SummarizeItem(BaseModel):
    """``id`` is the caller's opaque correlation handle, echoed back unchanged."""

    id: str
    documents: list[SourceDocument] = Field(min_length=1)


class SummarizeRequest(BaseModel):
    items: list[SummarizeItem] = Field(min_length=1)
    max_sentences: int | None = Field(default=None, gt=0)
    style: str | None = None


class Summary(BaseModel):
    """``summary`` is empty when ``withheld``. Branch on ``withheld``, not on the reason:
    ``withhold_reason`` may grow values without a wire-major bump.
    """

    id: str
    summary: str
    faithfulness_score: float = Field(ge=0.0, le=1.0)
    withheld: bool
    withhold_reason: WireWithholdReason | None = None
    provenance: list[str] = Field(default_factory=list)


class SummarizeResponse(BaseModel):
    results: list[Summary]
    errors: list[ErrorDetail] = Field(default_factory=list)
