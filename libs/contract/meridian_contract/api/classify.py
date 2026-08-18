"""``POST /v1/classify`` (RFC §8.1, PRD §8.1)."""

from pydantic import BaseModel, Field, field_validator

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

    @field_validator("items")
    @classmethod
    def _ids_are_unique(cls, items: list[ClassifyItem]) -> list[ClassifyItem]:
        """Results are matched back by ``id``, so a repeat makes the response ambiguous."""
        seen = {item.id for item in items}
        if len(seen) != len(items):
            raise ValueError("items[].id must be unique within a batch")
        return items


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
