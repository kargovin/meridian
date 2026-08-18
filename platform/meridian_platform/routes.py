"""The v1 routes.

Inference is canned (see ``stub``). The shapes, the batch ceilings, the error model and the
job lifecycle are real.
"""

import datetime as dt
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from meridian_contract.api import (
    CLASSIFY_MAX_BATCH,
    DEFAULT_TAXONOMY_VERSION,
    SUMMARIZE_SYNC_MAX_BATCH,
    SUPPORTED_TAXONOMY_VERSIONS,
    Classification,
    ClassifyRequest,
    ClassifyResponse,
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    JobAccepted,
    JobState,
    SummarizeRequest,
    SummarizeResponse,
)
from sqlalchemy.orm import Session

from meridian_platform.auth import Consumer
from meridian_platform.errors import PlatformError
from meridian_platform.jobs import as_response, enqueue, is_finished, read_job, run_sync
from meridian_platform.stub import classify_text

router = APIRouter(prefix="/v1")

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]

_RETRY_AFTER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying.",
        "schema": {"type": "integer"},
    }
}

_ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse},
    401: {"model": ErrorResponse, "description": "Missing or unusable bearer token."},
    429: {
        "model": ErrorResponse,
        "description": "Rate limited; honour Retry-After.",
        "headers": _RETRY_AFTER,
    },
}


def db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


Db = Annotated[Session, Depends(db)]


def _enforce(request: Request, consumer: str, bucket: str) -> None:
    retry_after = getattr(request.app.state, bucket).check(consumer)
    if retry_after is not None:
        raise PlatformError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            ErrorCode.RATE_LIMITED,
            "per-consumer rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )


def inference_limit(request: Request, consumer: Consumer) -> None:
    _enforce(request, consumer, "inference_limiter")


def poll_limit(request: Request, consumer: Consumer) -> None:
    _enforce(request, consumer, "poll_limiter")


@router.post(
    "/classify",
    response_model=ClassifyResponse,
    responses=_ERRORS
    | {
        400: {
            "model": ErrorResponse,
            "description": (
                "invalid_request: batch over max_batch, or a repeated items[].id. "
                "unsupported_taxonomy_version: a taxonomy_version this service does not serve."
            ),
        }
    },
    dependencies=[Depends(inference_limit)],
)
def classify(request: ClassifyRequest, consumer: Consumer) -> ClassifyResponse:
    """``Idempotency-Key`` is accepted and ignored: at a pinned model version a replay
    recomputes the same answer, so there is nothing to deduplicate.
    """
    if len(request.items) > CLASSIFY_MAX_BATCH:
        raise PlatformError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.INVALID_REQUEST,
            f"batch of {len(request.items)} exceeds max_batch {CLASSIFY_MAX_BATCH}",
        )

    version = request.taxonomy_version or DEFAULT_TAXONOMY_VERSION
    if version not in SUPPORTED_TAXONOMY_VERSIONS:
        # Answering against the current taxonomy would report a version we did not use.
        raise PlatformError(
            status.HTTP_400_BAD_REQUEST,
            ErrorCode.UNSUPPORTED_TAXONOMY_VERSION,
            f"taxonomy_version {version!r} is not served; supported:"
            f" {', '.join(SUPPORTED_TAXONOMY_VERSIONS)}",
        )

    results: list[Classification] = []
    errors: list[ErrorDetail] = []
    for item in request.items:
        outcome = classify_text(item.id, item.text)
        if outcome.error is not None:
            errors.append(outcome.error)
        elif outcome.result is not None:
            results.append(outcome.result)

    return ClassifyResponse(taxonomy_version=version, results=results, errors=errors)


@router.post(
    "/summarize",
    response_model=SummarizeResponse,
    responses={
        202: {
            "model": JobAccepted,
            "description": (
                "Batch above the sync ceiling, or a replay naming a job that has not finished."
            ),
        },
        **_ERRORS,
        400: {
            "model": ErrorResponse,
            "description": (
                "invalid_request: a repeated items[].id, or a style this service does not serve."
            ),
        },
    },
    dependencies=[Depends(inference_limit)],
)
def summarize(
    request: SummarizeRequest,
    request_context: Request,
    consumer: Consumer,
    session: Db,
    idempotency_key: IdempotencyKey = None,
) -> SummarizeResponse | JSONResponse:
    """Both paths write a job row; they differ in whether the answer is inline.

    A job that has not finished is answered 202 whichever path created it. An idempotency
    replay can reach the sync path naming a job that is still queued, and returning an empty
    ``results`` array for it would be success-shaped and false.
    """
    settings = request_context.app.state.settings
    retention = dt.timedelta(hours=settings.retention_hours)
    over_ceiling = len(request.items) > SUMMARIZE_SYNC_MAX_BATCH
    submit = enqueue if over_ceiling else run_sync

    job = submit(
        session,
        consumer,
        request.items,
        idempotency_key,
        retention=retention,
        max_sentences=request.max_sentences,
    )

    if not is_finished(job):
        accepted = JobAccepted(job_id=str(job.public_id), status=job.status)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=accepted.model_dump())

    return as_response(job)


@router.get(
    "/jobs/{job_id}",
    response_model=JobState,
    responses=_ERRORS | {404: {"model": ErrorResponse}},
    dependencies=[Depends(poll_limit)],
)
def job(job_id: str, consumer: Consumer, session: Db) -> JobState:
    state = read_job(session, job_id, consumer)
    if state is None:
        # Never-existed, swept, and another consumer's job answer identically, so the
        # endpoint is not an oracle for which job ids are real.
        raise PlatformError(
            status.HTTP_404_NOT_FOUND, ErrorCode.NOT_FOUND, "no such job", item_id=job_id
        )
    return state


__all__ = ["Response", "router"]
