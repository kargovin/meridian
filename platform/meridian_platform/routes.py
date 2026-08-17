"""The v1 routes.

Inference is canned. The responses are the real shapes and the real limits; the numbers
inside them are not model output. Stub behaviour is deterministic so a consumer can exercise
both branches of each field: an item whose text is shorter than ``_THIN_INPUT`` classifies as
a fallback and summarizes as withheld.
"""

from fastapi import APIRouter, Response, status
from meridian_contract.api import (
    CLASSIFY_MAX_BATCH,
    SUMMARIZE_SYNC_MAX_BATCH,
    Classification,
    ClassifyRequest,
    ClassifyResponse,
    ErrorCode,
    ErrorResponse,
    JobAccepted,
    JobState,
    SummarizeRequest,
    SummarizeResponse,
    Summary,
)

from meridian_platform.errors import PlatformError

router = APIRouter(prefix="/v1")

_THIN_INPUT = 200
_DEFAULT_TAXONOMY_VERSION = "v1"

_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse},
    429: {"model": ErrorResponse, "description": "Rate limited; honour Retry-After."},
}


@router.post("/classify", response_model=ClassifyResponse, responses=_ERROR_RESPONSES)
def classify(request: ClassifyRequest) -> ClassifyResponse:
    if len(request.items) > CLASSIFY_MAX_BATCH:
        raise PlatformError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.INVALID_REQUEST,
            f"batch of {len(request.items)} exceeds max_batch {CLASSIFY_MAX_BATCH}",
        )

    results = [
        Classification(
            id=item.id,
            topic="Other" if len(item.text) < _THIN_INPUT else "Technology",
            confidence=0.31 if len(item.text) < _THIN_INPUT else 0.94,
            fallback=len(item.text) < _THIN_INPUT,
        )
        for item in request.items
    ]
    return ClassifyResponse(
        taxonomy_version=request.taxonomy_version or _DEFAULT_TAXONOMY_VERSION,
        results=results,
    )


@router.post(
    "/summarize",
    response_model=SummarizeResponse,
    responses={
        202: {"model": JobAccepted, "description": "Batch above the sync ceiling."},
        **_ERROR_RESPONSES,
    },
)
def summarize(request: SummarizeRequest, response: Response) -> SummarizeResponse:
    if len(request.items) > SUMMARIZE_SYNC_MAX_BATCH:
        # The contract answers 202 here. Nothing yet stores a job, and a handle that never
        # resolves is worse than a refusal.
        raise PlatformError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            ErrorCode.INTERNAL,
            "the asynchronous job path is not available in this build",
        )

    results = []
    for item in request.items:
        thin = sum(len(document.text) for document in item.documents) < _THIN_INPUT
        results.append(
            Summary(
                id=item.id,
                summary="" if thin else "A canned summary standing in for model output.",
                faithfulness_score=0.42 if thin else 0.97,
                withheld=thin,
                withhold_reason="below_faithfulness_bar" if thin else None,
                provenance=[document.url for document in item.documents],
            )
        )
    return SummarizeResponse(results=results)


@router.get(
    "/jobs/{job_id}",
    response_model=JobState,
    responses=_ERROR_RESPONSES | {404: {"model": ErrorResponse}},
)
def job(job_id: str) -> JobState:
    # Never-existed, expired, and another consumer's job answer identically, so the endpoint
    # is not an oracle for which jobs exist.
    raise PlatformError(
        status.HTTP_404_NOT_FOUND, ErrorCode.NOT_FOUND, "no such job", item_id=job_id
    )
