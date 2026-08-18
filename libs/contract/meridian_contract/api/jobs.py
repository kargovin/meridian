"""``GET /v1/jobs/{job_id}`` (RFC §8.2)."""

from enum import StrEnum

from pydantic import BaseModel, Field

from meridian_contract.api.errors import ErrorDetail
from meridian_contract.api.summarize import Summary


class JobStatus(StrEnum):
    """Terminality is readable from this field alone; a caller polls until terminal."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    EXPIRED = "expired"


#: A job in any of these will not change again.
TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.EXPIRED}
)


class JobAccepted(BaseModel):
    """The 202 body. ``job_id`` is a random public handle, not a storage key."""

    job_id: str
    status: JobStatus


class JobState(BaseModel):
    """``results`` and ``errors`` are empty until the job reaches a terminal state.

    The documents you sent are discarded when the job reaches a terminal state; each
    result's ``provenance`` is kept, so the sources a summary drew from remain readable
    after the sources themselves are gone. The job is deleted 24 hours after it finishes; a
    poll in between answers ``expired``, and later still ``404 not_found``.
    """

    job_id: str
    status: JobStatus
    results: list[Summary] = Field(default_factory=list)
    errors: list[ErrorDetail] = Field(default_factory=list)
