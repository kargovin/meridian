"""The error envelope (RFC §8.2)."""

from enum import StrEnum

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Locked. Adding a member changes the published contract."""

    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_TAXONOMY_VERSION = "unsupported_taxonomy_version"
    ITEM_TOO_LARGE = "item_too_large"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    INTERNAL = "internal"


class ErrorDetail(BaseModel):
    """One failure. ``item_id`` is set only when the failure concerns a single batch item."""

    code: ErrorCode
    message: str
    item_id: str | None = None


class ErrorResponse(BaseModel):
    """A request that failed as a whole. Per-item failures go in a response's ``errors``."""

    error: ErrorDetail
