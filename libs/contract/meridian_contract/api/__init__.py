"""The wire contract. Imported by the Platform as the server and by the app as a client."""

from meridian_contract.api.classify import (
    CLASSIFY_MAX_BATCH,
    Classification,
    ClassifyItem,
    ClassifyRequest,
    ClassifyResponse,
)
from meridian_contract.api.errors import ErrorCode, ErrorDetail, ErrorResponse
from meridian_contract.api.jobs import (
    TERMINAL_JOB_STATUSES,
    JobAccepted,
    JobState,
    JobStatus,
)
from meridian_contract.api.summarize import (
    SUMMARIZE_SYNC_MAX_BATCH,
    SourceDocument,
    SummarizeItem,
    SummarizeRequest,
    SummarizeResponse,
    Summary,
    WireWithholdReason,
)

__all__ = [
    "CLASSIFY_MAX_BATCH",
    "SUMMARIZE_SYNC_MAX_BATCH",
    "TERMINAL_JOB_STATUSES",
    "Classification",
    "ClassifyItem",
    "ClassifyRequest",
    "ClassifyResponse",
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "JobAccepted",
    "JobState",
    "JobStatus",
    "SourceDocument",
    "SummarizeItem",
    "SummarizeRequest",
    "SummarizeResponse",
    "Summary",
    "WireWithholdReason",
]
