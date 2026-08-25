"""The admin surface over the runtime config plane (RFC §9)."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import AdminSettings, AppSettings
from sqlalchemy.orm import Session

from meridian.db import runtime_config
from meridian.db.models import RuntimeConfig
from meridian.db.runtime_config import POLL_INTERVAL_SECONDS
from meridian.web.app import create_app

pytestmark = pytest.mark.postgres

TOKEN = "t" * 32
AUTH = ("anything", TOKEN)
PATH = f"/admin/config/{POLL_INTERVAL_SECONDS.key}"


@pytest.fixture
def client(app_migrated: sa.Engine, app_session: Session) -> Iterator[TestClient]:
    app = create_app(
        settings=AppSettings.model_validate(
            {"database_url": app_migrated.url.render_as_string(hide_password=False)}
        ),
        admin=AdminSettings.model_validate({"token": TOKEN}),
    )
    with TestClient(app) as test_client:
        yield test_client


def _token(session: Session) -> str:
    session.expire_all()
    row = session.get(RuntimeConfig, POLL_INTERVAL_SECONDS.key)
    assert row is not None
    return row.updated_at.isoformat()


@pytest.mark.parametrize(("method", "path"), [("get", "/admin/config"), ("post", PATH)])
def test_the_config_routes_require_the_credential(
    client: TestClient, method: str, path: str
) -> None:
    assert getattr(client, method)(path).status_code == 401


def test_the_page_shows_the_current_value(client: TestClient) -> None:
    body = client.get("/admin/config", auth=AUTH).text
    assert POLL_INTERVAL_SECONDS.key in body
    assert 'value="300"' in body


def test_saving_a_value_changes_what_the_poller_reads(
    client: TestClient, app_session: Session
) -> None:
    """AC2 end to end: through the form an operator actually uses."""
    response = client.post(
        PATH,
        auth=AUTH,
        data={"value": "120", "expected_updated_at": _token(app_session)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    app_session.expire_all()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 120


def test_a_value_below_the_floor_is_refused(client: TestClient, app_session: Session) -> None:
    """The form carries min/max, so reaching the route with 5 means they were bypassed."""
    response = client.post(
        PATH,
        auth=AUTH,
        data={"value": "5", "expected_updated_at": _token(app_session)},
        follow_redirects=False,
    )
    assert response.status_code == 422
    app_session.expire_all()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 300


def test_a_page_rendered_before_an_out_of_band_change_cannot_write_over_it(
    client: TestClient, app_session: Session
) -> None:
    """The cadence is the primary freshness lever, and reverting it fails silently — the
    system keeps working and is simply staler than whoever made the first change intended.
    """
    stale = _token(app_session)
    client.post(
        PATH,
        auth=AUTH,
        data={"value": "900", "expected_updated_at": stale},
        follow_redirects=False,
    )

    response = client.post(
        PATH,
        auth=AUTH,
        data={"value": "60", "expected_updated_at": stale},
        follow_redirects=False,
    )

    assert response.status_code == 409
    app_session.expire_all()
    assert runtime_config.get_int(app_session, POLL_INTERVAL_SECONDS) == 900


def test_an_undeclared_key_is_not_writable(client: TestClient, app_session: Session) -> None:
    """The table is a config plane, not a place to store arbitrary rows through the web."""
    response = client.post(
        "/admin/config/anything_at_all",
        auth=AUTH,
        data={"value": "1", "expected_updated_at": _token(app_session)},
        follow_redirects=False,
    )
    assert response.status_code == 404


def test_a_rejected_stored_value_is_reported_not_displayed_as_running(
    client: TestClient, app_session: Session
) -> None:
    """⚠️ Reads fall back rather than raising, which is right — but a page that then renders the
    stored number shows a value nothing is using. For the one knob whose whole risk is a
    regression nobody notices, this page is where someone would look to check.
    """
    app_session.execute(
        sa.update(RuntimeConfig)
        .where(RuntimeConfig.key == POLL_INTERVAL_SECONDS.key)
        .values(value="7200")
    )
    app_session.commit()

    body = client.get("/admin/config", auth=AUTH).text

    assert 'value="300"' in body, "the page must show the value actually in force"
    assert "rejected" in body
    assert "<code>7200</code>" in body, "and must still say what was stored"


def test_an_unparseable_stored_value_is_reported_the_same_way(
    client: TestClient, app_session: Session
) -> None:
    app_session.execute(
        sa.update(RuntimeConfig)
        .where(RuntimeConfig.key == POLL_INTERVAL_SECONDS.key)
        .values(value="every five minutes")
    )
    app_session.commit()

    body = client.get("/admin/config", auth=AUTH).text

    assert 'value="300"' in body
    assert "rejected" in body


def test_a_usable_stored_value_is_shown_without_a_warning(
    client: TestClient, app_session: Session
) -> None:
    body = client.get("/admin/config", auth=AUTH).text

    assert 'value="300"' in body
    assert "rejected" not in body
