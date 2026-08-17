"""``POST /v1/classify`` (RFC §8.1, PRD §8.1)."""

from pydantic import BaseModel, Field

from meridian_contract.api.errors import ErrorDetail

#: Sync batch ceiling (2.3 / J4). There is no async path for classify.
CLASSIFY_MAX_BATCH = 64


class ClassifyItem(BaseModel):
    """``id`` is the caller's opaque correlation handle, echoed back unchanged."""

    id: str
    title: str
    text: str


class ClassifyRequest(BaseModel):
    items: list[ClassifyItem] = Field(min_length=1)
    taxonomy_version: str | None = None


class Classification(BaseModel):
    """``confidence`` is always returned, so no caller is forced through our threshold."""

    id: str
    topic: str
    confidence: float = Field(ge=0.0, le=1.0)
    fallback: bool


class ClassifyResponse(BaseModel):
    taxonomy_version: str
    results: list[Classification]
    errors: list[ErrorDetail] = Field(default_factory=list)
