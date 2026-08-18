"""Per-consumer rate limiting: the counter, and the response it produces."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import PlatformSettings
from meridian_contract.api import ErrorCode
from meridian_platform.limits import RateLimiter
from meridian_platform.main import create_app

pytestmark = pytest.mark.postgres

DIGEST = {"Authorization": "Bearer digest"}
MERIDIAN = {"Authorization": "Bearer meridian"}
ITEM = {"items": [{"id": "a1", "title": "t", "text": "x" * 500}]}


def test_the_counter_allows_up_to_the_limit() -> None:
    limiter = RateLimiter(limit=3, window=60.0)

    assert [limiter.check("digest", now=0.0) for _ in range(3)] == [None, None, None]


def test_the_counter_refuses_the_next_one() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check("digest", now=0.0)

    assert limiter.check("digest", now=0.0) == 60


def test_retry_after_shrinks_as_the_window_runs_out() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check("digest", now=0.0)

    assert limiter.check("digest", now=50.0) == 10


def test_retry_after_is_never_zero() -> None:
    """A Retry-After of 0 invites an immediate retry, which is the opposite of the point."""
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check("digest", now=0.0)

    assert limiter.check("digest", now=59.9) == 1


def test_the_window_resets() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check("digest", now=0.0)

    assert limiter.check("digest", now=60.0) is None


def test_one_consumer_does_not_spend_anothers_budget() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    limiter.check("digest", now=0.0)

    assert limiter.check("meridian", now=0.0) is None


@pytest.fixture
def strict_client(platform_migrated: sa.Engine) -> Iterator[TestClient]:
    settings = PlatformSettings(  # type: ignore[call-arg]
        database_url=platform_migrated.url.render_as_string(hide_password=False),  # type: ignore[arg-type]
        inference_rate_limit_per_minute=1,
        poll_rate_limit_per_minute=1,
        _env_file=None,
    )
    yield TestClient(create_app(settings, background=False))


def test_an_over_limit_call_is_refused_with_a_retry_after(strict_client: TestClient) -> None:
    strict_client.post("/v1/classify", json=ITEM, headers=DIGEST)

    response = strict_client.post("/v1/classify", json=ITEM, headers=DIGEST)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == ErrorCode.RATE_LIMITED
    assert int(response.headers["Retry-After"]) > 0


def test_the_poll_is_limited_too(strict_client: TestClient) -> None:
    """Without this a consumer picks a polling interval by guessing."""
    strict_client.get("/v1/jobs/whatever", headers=DIGEST)

    response = strict_client.get("/v1/jobs/whatever", headers=DIGEST)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_polling_does_not_spend_the_inference_budget(strict_client: TestClient) -> None:
    """Separate buckets: a 429 on the poll must not also mean 'you cannot submit work'."""
    strict_client.get("/v1/jobs/whatever", headers=DIGEST)
    strict_client.get("/v1/jobs/whatever", headers=DIGEST)

    assert strict_client.post("/v1/classify", json=ITEM, headers=DIGEST).status_code == 200


def test_a_second_consumer_is_unaffected(strict_client: TestClient) -> None:
    strict_client.post("/v1/classify", json=ITEM, headers=DIGEST)
    strict_client.post("/v1/classify", json=ITEM, headers=DIGEST)

    assert strict_client.post("/v1/classify", json=ITEM, headers=MERIDIAN).status_code == 200
