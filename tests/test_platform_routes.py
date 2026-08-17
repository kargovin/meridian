"""Route shapes and limits. Inference is canned; the shapes and ceilings are real."""

import pytest
from fastapi.testclient import TestClient
from meridian_config import PlatformSettings
from meridian_contract.api import CLASSIFY_MAX_BATCH, SUMMARIZE_SYNC_MAX_BATCH, ErrorCode
from meridian_platform.main import create_app

LONG = "x" * 500
SHORT = "x"


@pytest.fixture
def client() -> TestClient:
    settings = PlatformSettings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://platform:platform@localhost:5433/platform",  # type: ignore[arg-type]
        _env_file=None,
    )
    return TestClient(create_app(settings))


def classify_items(count: int, text: str = LONG) -> dict[str, object]:
    return {"items": [{"id": f"a{n}", "title": "t", "text": text} for n in range(count)]}


def summarize_items(count: int, text: str = LONG) -> dict[str, object]:
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


def test_classify_echoes_the_caller_ids(client: TestClient) -> None:
    body = client.post("/v1/classify", json=classify_items(3)).json()

    assert [result["id"] for result in body["results"]] == ["a0", "a1", "a2"]
    assert body["taxonomy_version"] == "v1"


def test_classify_returns_confidence_on_both_branches(client: TestClient) -> None:
    assigned = client.post("/v1/classify", json=classify_items(1)).json()["results"][0]
    fell_back = client.post("/v1/classify", json=classify_items(1, SHORT)).json()["results"][0]

    assert assigned["fallback"] is False
    assert fell_back["fallback"] is True
    assert 0.0 <= assigned["confidence"] <= 1.0
    assert 0.0 <= fell_back["confidence"] <= 1.0


def test_classify_refuses_a_batch_over_the_ceiling(client: TestClient) -> None:
    response = client.post("/v1/classify", json=classify_items(CLASSIFY_MAX_BATCH + 1))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.INVALID_REQUEST


def test_summarize_returns_the_faithfulness_signal(client: TestClient) -> None:
    result = client.post("/v1/summarize", json=summarize_items(1)).json()["results"][0]

    assert result["withheld"] is False
    assert result["withhold_reason"] is None
    assert result["provenance"] == ["https://e/1"]


def test_a_withheld_summary_names_the_only_reason_this_service_produces(
    client: TestClient,
) -> None:
    result = client.post("/v1/summarize", json=summarize_items(1, SHORT)).json()["results"][0]

    assert result["withheld"] is True
    assert result["withhold_reason"] == "below_faithfulness_bar"
    assert result["summary"] == ""


def test_summarize_over_the_sync_ceiling_is_not_answered_inline(client: TestClient) -> None:
    """The contract answers 202 here once jobs are stored."""
    response = client.post("/v1/summarize", json=summarize_items(SUMMARIZE_SYNC_MAX_BATCH + 1))

    assert response.status_code != 200


def test_an_unknown_job_is_not_found(client: TestClient) -> None:
    response = client.get("/v1/jobs/whatever")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.NOT_FOUND
