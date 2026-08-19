"""The admin surface's HTTP behaviour (FR-I2, FR-I6, RFC §9)."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import AdminSettings, AppSettings
from sqlalchemy.orm import Session

from meridian.db import sources
from meridian.web.app import create_app

TOKEN = "t" * 32
AUTH = ("anything", TOKEN)

FORM = {
    "name": "Example Times",
    "home_url": "https://times.example",
    "discovery_method": "rss",
    "acquisition_tier": "1_full_feed",
    "rights_level": "body_text",
    "jurisdiction": "GB",
    "rate_limit_per_min": "20",
}


@pytest.fixture
def client(app_migrated: sa.Engine, app_session: Session) -> Iterator[TestClient]:
    """A client sharing the test session's database.

    ``app_session`` is depended on for its truncation, so each test starts with an empty
    registry.
    """
    app = create_app(
        settings=AppSettings.model_validate(
            {"database_url": app_migrated.url.render_as_string(hide_password=False)}
        ),
        admin=AdminSettings.model_validate({"token": TOKEN}),
    )
    with TestClient(app) as test_client:
        yield test_client


# ------------------------------------------------------------------ the credential


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/admin/sources"),
        ("get", "/admin/sources/new"),
        ("get", "/admin/sources/1"),
        ("post", "/admin/sources"),
        ("post", "/admin/sources/1"),
        ("post", "/admin/sources/1/enable"),
        ("post", "/admin/sources/1/tier"),
    ],
)
def test_every_admin_route_requires_the_credential(
    client: TestClient, method: str, path: str
) -> None:
    """Parametrized over the routes rather than spot-checked.

    The dependency is declared once on the router, so a route added later inherits it — but
    only until somebody builds a second router. Listing the paths here means a route that
    escapes the guard fails a test instead of being noticed by a reader.
    """
    assert getattr(client, method)(path).status_code == 401


def test_the_challenge_names_basic_so_a_browser_prompts(client: TestClient) -> None:
    """Without WWW-Authenticate the browser never asks, and nobody can get in."""
    header = client.get("/admin/sources").headers["WWW-Authenticate"]

    assert header.startswith("Basic ")


def test_a_wrong_credential_is_rejected(client: TestClient) -> None:
    assert client.get("/admin/sources", auth=("admin", "wrong")).status_code == 401


def test_the_username_is_not_part_of_the_credential(client: TestClient) -> None:
    """One shared secret, so requiring a name would imply an attribution it cannot make."""
    assert client.get("/admin/sources", auth=("alice", TOKEN)).status_code == 200
    assert client.get("/admin/sources", auth=("bob", TOKEN)).status_code == 200


# ------------------------------------------------------------------ routing


def test_the_create_form_is_reachable(client: TestClient) -> None:
    """``/sources/new`` must not be swallowed by the typed ``{source_id}`` route.

    A typed path parameter rejects rather than falling through, so declaring them the other
    way round makes this URL answer 422 forever.
    """
    assert client.get("/admin/sources/new", auth=AUTH).status_code == 200


def test_an_unknown_source_is_a_404(client: TestClient) -> None:
    assert client.get("/admin/sources/999999", auth=AUTH).status_code == 404
    assert (
        client.post("/admin/sources/999999/enable", auth=AUTH, data={"enabled": "true"}).status_code
        == 404
    )


# ------------------------------------------------------------------ writes


def test_create_persists_and_redirects(client: TestClient, app_session: Session) -> None:
    response = client.post("/admin/sources", auth=AUTH, data=FORM, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/sources"
    assert [s.name for s in sources.list_all(app_session)] == ["Example Times"]


def test_a_write_answers_303_not_302(client: TestClient) -> None:
    """303 tells the browser to follow with GET.

    Under 302 the method is the browser's choice, and re-POSTing the form on reload is how a
    single disable becomes two.
    """
    created = client.post("/admin/sources", auth=AUTH, data=FORM, follow_redirects=False)

    assert created.status_code == 303


def test_disabling_a_source_takes_effect_immediately(
    client: TestClient, app_session: Session
) -> None:
    """AC1 end to end: no deploy, no restart, and the next read of the poll set excludes it."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]
    assert [s.source_id for s in sources.enabled(app_session)] == [source.source_id]

    client.post(f"/admin/sources/{source.source_id}/enable", auth=AUTH, data={"enabled": "false"})

    app_session.expire_all()
    assert sources.enabled(app_session) == []


def test_a_tier_downgrade_takes_effect_immediately(
    client: TestClient, app_session: Session
) -> None:
    """AC3, by the same path as AC1."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]

    client.post(
        f"/admin/sources/{source.source_id}/tier",
        auth=AUTH,
        data={"acquisition_tier": "3_extraction"},
    )

    app_session.expire_all()
    refreshed = sources.get(app_session, source.source_id)
    assert refreshed is not None
    assert refreshed.acquisition_tier.value == "3_extraction"


def test_an_invalid_enum_value_is_rejected(client: TestClient, app_session: Session) -> None:
    """The vocabulary is the contract's, not free text — the CHECK would refuse it anyway,
    and a 422 is a better answer than a 500 from the database."""
    response = client.post(
        "/admin/sources", auth=AUTH, data={**FORM, "discovery_method": "carrier_pigeon"}
    )

    assert response.status_code == 422
    assert sources.list_all(app_session) == []


# ------------------------------------------------------------------ the home_url constraint


def test_a_duplicate_home_url_is_refused_without_a_500(
    client: TestClient, app_session: Session
) -> None:
    """One row per publisher.

    Two rows carry two source_ids, so one story arriving through both counts twice in a
    cluster's distinct-source total and promotes a single-publisher cluster past the
    >=2-source gate (FR-S6). The constraint is the guard; this is the guard being reported
    rather than crashing.
    """
    assert client.post("/admin/sources", auth=AUTH, data=FORM).status_code == 200

    response = client.post("/admin/sources", auth=AUTH, data={**FORM, "name": "Same Site"})

    assert response.status_code == 409
    assert "already exists" in response.text
    assert len(sources.list_all(app_session)) == 1


def test_a_refused_create_keeps_what_was_typed(client: TestClient) -> None:
    """Answering 409 and clearing the form makes the operator retype eight fields."""
    client.post("/admin/sources", auth=AUTH, data=FORM)

    body = client.post(
        "/admin/sources", auth=AUTH, data={**FORM, "name": "Same Site", "jurisdiction": "FR"}
    ).text

    assert "Same Site" in body
    assert 'value="FR"' in body


def test_a_refused_create_still_posts_to_the_create_url(client: TestClient) -> None:
    """The re-rendered form carries an unsaved Source with no id.

    Derived from the object rather than passed in, the action would point at the edit URL of
    a row that was never created, and the retry would 404.
    """
    client.post("/admin/sources", auth=AUTH, data=FORM)

    body = client.post("/admin/sources", auth=AUTH, data={**FORM, "name": "Same"}).text

    assert 'action="/admin/sources"' in body


def test_renaming_a_source_onto_another_url_is_refused(
    client: TestClient, app_session: Session
) -> None:
    client.post("/admin/sources", auth=AUTH, data=FORM)
    client.post(
        "/admin/sources", auth=AUTH, data={**FORM, "name": "Other", "home_url": "https://b.example"}
    )
    target = next(s for s in sources.list_all(app_session) if s.name == "Other")

    response = client.post(
        f"/admin/sources/{target.source_id}",
        auth=AUTH,
        data={**FORM, "name": "Other", "enabled": "true"},
    )

    assert response.status_code == 409
