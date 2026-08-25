"""The admin surface's HTTP behaviour (FR-I2, FR-I6, RFC §9)."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import AdminSettings, AppSettings
from sqlalchemy.orm import Session

from meridian.db import feeds, sources
from meridian.web.app import create_app


def _token(session: Session, source_id: int) -> str:
    """The version token a freshly rendered page would carry."""
    session.expire_all()
    source = sources.get(session, source_id)
    assert source is not None
    return source.updated_at.isoformat()


TOKEN = "t" * 32
AUTH = ("anything", TOKEN)

FORM = {
    "name": "Example Times",
    "home_url": "https://times.example",
    "rights_level": "body_text",
    "jurisdiction": "GB",
    "rate_limit_per_min": "20",
}

FEED_FORM = {
    "name": "World",
    "url": "https://feeds.times.example/world.xml",
    "discovery_method": "rss",
    "acquisition_tier": "1_full_feed",
}


def _feed_token(session: Session, feed_id: int) -> str:
    session.expire_all()
    feed = feeds.get(session, feed_id)
    assert feed is not None
    return feed.updated_at.isoformat()


def _make_publisher_with_feed(client: TestClient, session: Session) -> tuple[int, int]:
    """The pair every feed test needs: a publisher id and one of its feed ids."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(session)[0]
    client.post(f"/admin/sources/{source.source_id}/feeds", auth=AUTH, data=FEED_FORM)
    session.expire_all()
    feed = feeds.for_source(session, source.source_id)[0]
    return source.source_id, feed.feed_id


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
        ("post", "/admin/sources/1/rights"),
        ("post", "/admin/sources/1/enable"),
        ("post", "/admin/sources/1/permitted"),
        ("post", "/admin/sources/1/feeds"),
        ("post", "/admin/feeds/1"),
        ("post", "/admin/feeds/1/enable"),
        ("post", "/admin/feeds/1/tier"),
        ("post", "/admin/feeds/1/delete"),
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
        client.post(
            "/admin/sources/999999/enable",
            auth=AUTH,
            data={"enabled": "true", "expected_updated_at": "2026-01-01T00:00:00+00:00"},
        ).status_code
        == 404
    )


# ------------------------------------------------------------------ writes


def test_create_persists_and_redirects(client: TestClient, app_session: Session) -> None:
    response = client.post("/admin/sources", auth=AUTH, data=FORM, follow_redirects=False)

    assert response.status_code == 303
    created = sources.list_all(app_session)
    assert [s.name for s in created] == ["Example Times"]
    # To the publisher's own page, not the list: it has no feeds yet, so it polls nothing.
    assert response.headers["location"] == f"/admin/sources/{created[0].source_id}"


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

    response = client.post(
        f"/admin/sources/{source.source_id}/enable",
        auth=AUTH,
        data={"enabled": "false", "expected_updated_at": _token(app_session, source.source_id)},
    )

    assert response.status_code == 200, response.text
    app_session.expire_all()
    assert sources.enabled(app_session) == []


def test_a_rights_revocation_takes_effect_immediately(
    client: TestClient, app_session: Session
) -> None:
    """Its own route, for the same reason as the enable toggle (FR-S5)."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]

    client.post(
        f"/admin/sources/{source.source_id}/rights",
        auth=AUTH,
        data={
            "rights_level": "headline_only",
            "expected_updated_at": _token(app_session, source.source_id),
        },
    )

    app_session.expire_all()
    refreshed = sources.get(app_session, source.source_id)
    assert refreshed is not None
    assert refreshed.rights_level.value == "headline_only"


def test_the_edit_form_cannot_revert_an_emergency_change(
    client: TestClient, app_session: Session
) -> None:
    """The clobber the split exists to prevent.

    A form rendered before a stop-ingestion change and submitted after it must not re-enable
    the source. Routed through a full-row write it would, with a 303 and no error, from one
    operator with two tabs open.
    """
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]
    stale = {
        "name": FORM["name"],
        "home_url": FORM["home_url"],
        "jurisdiction": FORM["jurisdiction"],
        "rate_limit_per_min": FORM["rate_limit_per_min"],
        "enabled": "true",
        "permitted_to_ingest": "true",
        "rights_level": "body_text",
    }
    client.post(
        f"/admin/sources/{source.source_id}/enable",
        auth=AUTH,
        data={"enabled": "false", "expected_updated_at": _token(app_session, source.source_id)},
    )
    client.post(
        f"/admin/sources/{source.source_id}/rights",
        auth=AUTH,
        data={
            "rights_level": "headline_only",
            "expected_updated_at": _token(app_session, source.source_id),
        },
    )

    assert (
        client.post(f"/admin/sources/{source.source_id}", auth=AUTH, data=stale).status_code == 200
    )

    app_session.expire_all()
    after = sources.get(app_session, source.source_id)
    assert after is not None
    assert after.enabled is False, "the stale form re-enabled a stopped source"
    assert after.rights_level.value == "headline_only", "the stale form restored body rights"
    assert after.permitted_to_ingest is True, "unrelated: the withdrawal path is its own test"


@pytest.mark.parametrize("bad", ["0", "-5"])
def test_a_non_positive_rate_limit_is_refused(
    client: TestClient, app_session: Session, bad: str
) -> None:
    """FR-I3 politeness has no meaning at zero; the markup's min= is only a hint."""
    response = client.post("/admin/sources", auth=AUTH, data={**FORM, "rate_limit_per_min": bad})

    assert response.status_code == 422
    assert sources.list_all(app_session) == []


def test_the_app_publishes_no_api(client: TestClient) -> None:
    """A1 makes the Platform the published contract; this document only indexed admin routes,
    and was not covered by the per-route credential."""
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path


def test_a_tier_downgrade_takes_effect_immediately(
    client: TestClient, app_session: Session
) -> None:
    """AC3, by the same path as AC1 — on the feed, which is what a tier is a fact about."""
    _, feed_id = _make_publisher_with_feed(client, app_session)

    client.post(
        f"/admin/feeds/{feed_id}/tier",
        auth=AUTH,
        data={
            "acquisition_tier": "3_extraction",
            "expected_updated_at": _feed_token(app_session, feed_id),
        },
    )

    app_session.expire_all()
    refreshed = feeds.get(app_session, feed_id)
    assert refreshed is not None
    assert refreshed.acquisition_tier.value == "3_extraction"


def test_withdrawing_permission_takes_effect_immediately(
    client: TestClient, app_session: Session
) -> None:
    """The Legal stop, on its own route and separate from the operational one."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]

    client.post(
        f"/admin/sources/{source.source_id}/permitted",
        auth=AUTH,
        data={
            "permitted_to_ingest": "false",
            "expected_updated_at": _token(app_session, source.source_id),
        },
    )

    app_session.expire_all()
    refreshed = sources.get(app_session, source.source_id)
    assert refreshed is not None
    assert refreshed.permitted_to_ingest is False
    assert refreshed.enabled is True, "the operational switch is a different column"
    assert sources.enabled(app_session) == []


def test_a_feed_write_returns_to_its_publisher(client: TestClient, app_session: Session) -> None:
    """Not to the publisher list: the feed just edited is on the publisher's page."""
    source_id, feed_id = _make_publisher_with_feed(client, app_session)

    response = client.post(
        f"/admin/feeds/{feed_id}/enable",
        auth=AUTH,
        data={"enabled": "false", "expected_updated_at": _feed_token(app_session, feed_id)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/sources/{source_id}"


def test_deleting_a_feed_leaves_the_publisher(client: TestClient, app_session: Session) -> None:
    source_id, feed_id = _make_publisher_with_feed(client, app_session)

    client.post(f"/admin/feeds/{feed_id}/delete", auth=AUTH)

    app_session.expire_all()
    assert feeds.for_source(app_session, source_id) == []
    assert sources.get(app_session, source_id) is not None


def test_an_unknown_feed_is_a_404(client: TestClient) -> None:
    for path, data in (
        ("/admin/feeds/999999/delete", {}),
        (
            "/admin/feeds/999999/enable",
            {"enabled": "true", "expected_updated_at": "2026-01-01T00:00:00+00:00"},
        ),
    ):
        assert client.post(path, auth=AUTH, data=data).status_code == 404, path


def test_a_feed_cannot_be_added_to_an_unknown_publisher(client: TestClient) -> None:
    """Without the check the foreign key answers instead, as a 500."""
    response = client.post("/admin/sources/999999/feeds", auth=AUTH, data=FEED_FORM)

    assert response.status_code == 404


def test_an_invalid_enum_value_is_rejected(client: TestClient, app_session: Session) -> None:
    """The vocabulary is the contract's, not free text — the CHECK would refuse it anyway,
    and a 422 is a better answer than a 500 from the database."""
    client.post("/admin/sources", auth=AUTH, data=FORM)
    source = sources.list_all(app_session)[0]

    response = client.post(
        f"/admin/sources/{source.source_id}/feeds",
        auth=AUTH,
        data={**FEED_FORM, "discovery_method": "carrier_pigeon"},
    )

    assert response.status_code == 422
    assert feeds.for_source(app_session, source.source_id) == []
