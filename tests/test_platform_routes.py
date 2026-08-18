"""Route behaviour over HTTP: limits, the error envelope, auth, and the job path."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import PlatformSettings
from meridian_contract.api import CLASSIFY_MAX_BATCH, SUMMARIZE_SYNC_MAX_BATCH, ErrorCode
from meridian_platform.jobs import process_next
from meridian_platform.main import create_app
from meridian_platform.stub import THIN_INPUT
from sqlalchemy.orm import Session

pytestmark = pytest.mark.postgres

LONG = "x" * (THIN_INPUT + 300)
SHORT = "x"
DIGEST = {"Authorization": "Bearer digest"}


@pytest.fixture
def client(platform_migrated: sa.Engine, platform_session: Session) -> Iterator[TestClient]:
    """A client on the test database. ``background=False``: this test drives the worker."""
    settings = PlatformSettings(  # type: ignore[call-arg]
        database_url=platform_migrated.url.render_as_string(hide_password=False),  # type: ignore[arg-type]
        _env_file=None,
    )
    yield TestClient(create_app(settings, background=False))


def classify_body(count: int, text: str = LONG) -> dict[str, object]:
    return {"items": [{"id": f"a{n}", "title": "t", "text": text} for n in range(count)]}


def summarize_body(count: int, text: str = LONG) -> dict[str, object]:
    return {
        "items": [
            {
                "id": f"c{n}",
                "documents": [
                    {"source": "outlet-a", "title": "t", "text": text, "url": "https://e/1"}
                ],
            }
            for n in range(count)
        ]
    }


def test_a_request_without_a_token_is_rejected(client: TestClient) -> None:
    response = client.post("/v1/classify", json=classify_body(1))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_classify_echoes_the_caller_ids(client: TestClient) -> None:
    body = client.post("/v1/classify", json=classify_body(3), headers=DIGEST).json()

    assert [result["id"] for result in body["results"]] == ["a0", "a1", "a2"]
    assert body["taxonomy_version"] == "v1"


def test_classify_returns_confidence_on_both_branches(client: TestClient) -> None:
    assigned = client.post("/v1/classify", json=classify_body(1), headers=DIGEST).json()
    fell_back = client.post("/v1/classify", json=classify_body(1, SHORT), headers=DIGEST).json()

    assert assigned["results"][0]["fallback"] is False
    assert fell_back["results"][0]["fallback"] is True


def test_classify_refuses_a_batch_over_the_ceiling(client: TestClient) -> None:
    response = client.post(
        "/v1/classify", json=classify_body(CLASSIFY_MAX_BATCH + 1), headers=DIGEST
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_summarize_returns_the_faithfulness_signal(client: TestClient) -> None:
    result = client.post("/v1/summarize", json=summarize_body(1), headers=DIGEST).json()

    assert result["results"][0]["withheld"] is False
    assert result["results"][0]["withhold_reason"] is None


def test_a_withheld_summary_names_the_only_reason_this_service_produces(
    client: TestClient,
) -> None:
    body = client.post("/v1/summarize", json=summarize_body(1, SHORT), headers=DIGEST).json()

    assert body["results"][0]["withheld"] is True
    assert body["results"][0]["withhold_reason"] == "below_faithfulness_bar"


def test_a_batch_over_the_sync_ceiling_is_accepted_as_a_job(client: TestClient) -> None:
    response = client.post(
        "/v1/summarize", json=summarize_body(SUMMARIZE_SYNC_MAX_BATCH + 1), headers=DIGEST
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_a_job_can_be_polled_to_completion(client: TestClient, platform_session: Session) -> None:
    job_id = client.post(
        "/v1/summarize", json=summarize_body(SUMMARIZE_SYNC_MAX_BATCH + 1), headers=DIGEST
    ).json()["job_id"]

    assert client.get(f"/v1/jobs/{job_id}", headers=DIGEST).json()["status"] == "queued"

    process_next(platform_session)

    finished = client.get(f"/v1/jobs/{job_id}", headers=DIGEST).json()
    assert finished["status"] == "succeeded"
    assert len(finished["results"]) == SUMMARIZE_SYNC_MAX_BATCH + 1


def test_another_consumer_polling_the_same_handle_gets_nothing(client: TestClient) -> None:
    job_id = client.post(
        "/v1/summarize", json=summarize_body(SUMMARIZE_SYNC_MAX_BATCH + 1), headers=DIGEST
    ).json()["job_id"]

    response = client.get(f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer meridian"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND


def test_an_unknown_job_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/jobs/whatever", headers=DIGEST)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND


def test_a_retried_batch_is_not_run_twice(client: TestClient) -> None:
    headers = DIGEST | {"Idempotency-Key": "batch-42"}
    body = summarize_body(SUMMARIZE_SYNC_MAX_BATCH + 1)

    first = client.post("/v1/summarize", json=body, headers=headers).json()
    second = client.post("/v1/summarize", json=body, headers=headers).json()

    assert first["job_id"] == second["job_id"]
