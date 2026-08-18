"""Route behaviour over HTTP: limits, the error envelope, auth, and the job path."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import PlatformSettings
from meridian_contract.api import CLASSIFY_MAX_BATCH, SUMMARIZE_SYNC_MAX_BATCH, ErrorCode
from meridian_platform.db import SummarizeJob
from meridian_platform.jobs import process_next
from meridian_platform.main import create_app
from meridian_platform.stub import OVERSIZED, THIN_INPUT
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


def test_a_retried_batch_is_not_run_twice(client: TestClient, platform_session: Session) -> None:
    """Matching job ids is not the property: nothing may be inferred a second time."""
    headers = DIGEST | {"Idempotency-Key": "batch-42"}
    body = summarize_body(SUMMARIZE_SYNC_MAX_BATCH + 1)

    first = client.post("/v1/summarize", json=body, headers=headers).json()
    second = client.post("/v1/summarize", json=body, headers=headers).json()

    assert first["job_id"] == second["job_id"]
    jobs = platform_session.scalars(sa.select(SummarizeJob)).all()
    assert len(jobs) == 1
    assert jobs[0].attempts == 0, "a replay must not queue a second run"

    assert process_next(platform_session) is True
    assert process_next(platform_session) is False, "the batch was runnable twice"


def test_an_unknown_path_uses_the_locked_envelope(client: TestClient) -> None:
    """Starlette's router answers before any of our handlers unless one catches it."""
    response = client.get("/v1/nothing-here", headers=DIGEST)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND
    assert "detail" not in response.json()


def test_an_unsupported_method_uses_the_locked_envelope(client: TestClient) -> None:
    response = client.delete("/v1/classify", headers=DIGEST)

    assert response.status_code == 405
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_a_malformed_request_uses_the_locked_envelope(client: TestClient) -> None:
    """FastAPI's default answers 422 with {"detail": [...]}, a second error shape."""
    response = client.post("/v1/classify", json={"items": []}, headers=DIGEST)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST
    assert "detail" not in response.json()


def test_duplicate_item_ids_are_a_caller_error(client: TestClient) -> None:
    """They also violate UNIQUE(job_id, item_id), which would surface as an unhandled 500."""
    body = {"items": [{"id": "same", "title": "t", "text": LONG} for _ in range(2)]}

    response = client.post("/v1/classify", json=body, headers=DIGEST)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_duplicate_summarize_item_ids_are_a_caller_error(client: TestClient) -> None:
    body = summarize_body(2)
    body["items"][1]["id"] = body["items"][0]["id"]  # type: ignore[index]

    response = client.post("/v1/summarize", json=body, headers=DIGEST)

    assert response.status_code == 400


def test_an_unexpected_failure_still_uses_the_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`internal` must have a producer, and a plain-text 500 is outside the contract."""
    import meridian_platform.routes as routes

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated")

    monkeypatch.setattr(routes, "classify_text", boom)
    lenient = TestClient(client.app, raise_server_exceptions=False)

    response = lenient.post("/v1/classify", json=classify_body(1), headers=DIGEST)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == ErrorCode.INTERNAL


def test_an_unserved_taxonomy_version_is_refused(client: TestClient) -> None:
    """Echoing it back as honoured reports a version we did not classify against."""
    body = classify_body(1) | {"taxonomy_version": "v99-does-not-exist"}

    response = client.post("/v1/classify", json=body, headers=DIGEST)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.UNSUPPORTED_TAXONOMY_VERSION


def test_a_served_taxonomy_version_is_accepted(client: TestClient) -> None:
    body = classify_body(1) | {"taxonomy_version": "v1"}

    assert client.post("/v1/classify", json=body, headers=DIGEST).status_code == 200


def test_classify_reports_a_bad_item_without_failing_the_batch(client: TestClient) -> None:
    body = classify_body(2)
    body["items"][1]["text"] = "x" * (OVERSIZED + 1)  # type: ignore[index]

    payload = client.post("/v1/classify", json=body, headers=DIGEST).json()

    assert [result["id"] for result in payload["results"]] == ["a0"]
    assert payload["errors"][0]["code"] == ErrorCode.ITEM_TOO_LARGE
    assert payload["errors"][0]["item_id"] == "a1"


def test_an_unsupported_style_is_refused(client: TestClient) -> None:
    """Accepted-and-ignored is the failure mode: the caller thinks it took effect."""
    body = summarize_body(1) | {"style": "breezy"}

    assert client.post("/v1/summarize", json=body, headers=DIGEST).status_code == 400


def test_max_sentences_is_honoured(client: TestClient) -> None:
    one = summarize_body(1) | {"max_sentences": 1}
    three = summarize_body(1) | {"max_sentences": 3}

    short = client.post("/v1/summarize", json=one, headers=DIGEST).json()
    long = client.post("/v1/summarize", json=three, headers=DIGEST).json()

    assert len(short["results"][0]["summary"]) < len(long["results"][0]["summary"])


def test_a_summary_reports_the_sources_it_drew_from(client: TestClient) -> None:
    """FR-S3. The documents are discarded, so this cannot be recovered later."""
    payload = client.post("/v1/summarize", json=summarize_body(1), headers=DIGEST).json()

    assert payload["results"][0]["provenance"] == ["https://e/1"]


def test_a_replay_naming_an_unfinished_job_is_not_a_result(
    client: TestClient, platform_session: Session
) -> None:
    """The empty-but-successful answer is the one a caller cannot detect."""
    headers = DIGEST | {"Idempotency-Key": "shared-key"}
    client.post("/v1/summarize", json=summarize_body(3), headers=headers)

    replay = client.post("/v1/summarize", json=summarize_body(1), headers=headers)

    assert replay.status_code == 202
    assert replay.json()["status"] == "queued"


def test_the_rate_limit_response_documents_retry_after(client: TestClient) -> None:
    document = client.get("/openapi.json").json()
    limited = document["paths"]["/v1/classify"]["post"]["responses"]["429"]

    assert "Retry-After" in limited["headers"]
