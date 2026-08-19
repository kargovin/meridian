"""What the admin pages actually render.

The route tests assert status codes and redirects, which pass whatever the markup says. These
assert on the page.
"""

from collections.abc import Iterator
from enum import StrEnum
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from meridian_config import AdminSettings, AppSettings
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db import sources
from meridian.web.app import create_app
from tests.factories import make_source

TOKEN = "t" * 32
AUTH = ("anything", TOKEN)


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


class _Form(HTMLParser):
    """The fields of the first <form> whose action matches, as a submittable dict.

    A browser submits successful controls: text and number inputs always, checkboxes only
    when checked, and the selected option of each select. Reproducing that is the point —
    it is what makes a round trip a real test of the template against the route.
    """

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.fields: dict[str, str] = {}
        self._in_form = False
        self._select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._in_form = a.get("action") == self.action
        elif not self._in_form:
            return
        elif tag == "input":
            name, kind = a.get("name"), a.get("type", "text")
            if name and (kind != "checkbox" or "checked" in a):
                self.fields[name] = a.get("value", "")
        elif tag == "select":
            self._select = a.get("name")
        elif tag == "option" and self._select and "selected" in a:
            self.fields[self._select] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._select = None
        elif tag == "form":
            self._in_form = False


def _form(html: str, action: str) -> dict[str, str]:
    parser = _Form(action)
    parser.feed(html)
    return parser.fields


# ------------------------------------------------------------------ the list


def test_the_list_shows_a_source(client: TestClient, app_session: Session) -> None:
    make_source(app_session, name="Example Times")
    app_session.commit()

    body = client.get("/admin/sources", auth=AUTH).text

    assert "Example Times" in body


def test_the_empty_list_says_so_rather_than_rendering_nothing(client: TestClient) -> None:
    assert "No sources yet" in client.get("/admin/sources", auth=AUTH).text


def test_a_source_name_is_escaped(client: TestClient, app_session: Session) -> None:
    """``name`` arrives from a form, so it is attacker-controlled in principle."""
    make_source(app_session, name="<script>alert(1)</script>")
    app_session.commit()

    body = client.get("/admin/sources", auth=AUTH).text

    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_the_toggle_posts_the_opposite_of_the_current_state(
    client: TestClient, app_session: Session
) -> None:
    """The button that reads Stop must send enabled=false, and vice versa.

    Inverted, the control does the opposite of its own label and every test that only checks
    a status code still passes.
    """
    on = make_source(app_session, name="Running")
    off = make_source(app_session, name="Stopped", enabled=False)
    app_session.commit()

    body = client.get("/admin/sources", auth=AUTH).text

    assert _form(body, f"/admin/sources/{on.source_id}/enable")["enabled"] == "false"
    assert _form(body, f"/admin/sources/{off.source_id}/enable")["enabled"] == "true"


def test_every_governing_control_carries_the_version_token(
    client: TestClient, app_session: Session
) -> None:
    """Without it the control writes the value it rendered, whatever happened since."""
    source = make_source(app_session, name="Example")
    app_session.commit()

    body = client.get("/admin/sources", auth=AUTH).text

    for action in ("enable", "rights", "tier"):
        fields = _form(body, f"/admin/sources/{source.source_id}/{action}")
        assert "expected_updated_at" in fields, action
        assert fields["expected_updated_at"] == source.updated_at.isoformat()


def test_headline_only_is_called_out_in_the_list(client: TestClient, app_session: Session) -> None:
    """The field that decides whether we may summarize should not read like the others."""
    make_source(app_session, name="Wire", rights_level=RightsLevel.HEADLINE_ONLY)
    app_session.commit()

    assert "headline only" in client.get("/admin/sources", auth=AUTH).text


# ------------------------------------------------------------------ the form


@pytest.mark.parametrize(
    "vocabulary", [list(DiscoveryMethod), list(AcquisitionTier), list(RightsLevel)]
)
def test_the_form_offers_every_member_of_the_contract_vocabulary(
    client: TestClient, vocabulary: list[StrEnum]
) -> None:
    """Options come from the contract enums, not from literals in the markup.

    Repeated as literals they drift from the CHECK constraint built off the same enum, and
    the form then offers a value the database rejects — or omits one it accepts.
    """
    body = client.get("/admin/sources/new", auth=AUTH).text

    for member in vocabulary:
        assert f'value="{member.value}"' in body


def test_the_edit_form_is_filled_in_with_the_current_values(
    client: TestClient, app_session: Session
) -> None:
    source = make_source(
        app_session,
        name="Example Wire",
        discovery_method=DiscoveryMethod.SITEMAP,
        acquisition_tier=AcquisitionTier.EXTRACTION,
        rights_level=RightsLevel.HEADLINE_ONLY,
        jurisdiction="US",
        rate_limit_per_min=5,
    )
    app_session.commit()

    fields = _form(
        client.get(f"/admin/sources/{source.source_id}", auth=AUTH).text,
        f"/admin/sources/{source.source_id}",
    )

    assert fields["name"] == "Example Wire"
    assert fields["discovery_method"] == "sitemap"
    assert fields["jurisdiction"] == "US"
    assert fields["rate_limit_per_min"] == "5"
    # Governing fields are set from the list page, never from this form.
    assert "acquisition_tier" not in fields
    assert "rights_level" not in fields
    assert "enabled" not in fields


def test_a_disabled_source_renders_an_unchecked_box(
    client: TestClient, app_session: Session
) -> None:
    """An unchecked checkbox is absent from the submission — that is how the route reads it."""
    source = make_source(app_session, name="Stopped", enabled=False)
    app_session.commit()

    fields = _form(
        client.get(f"/admin/sources/{source.source_id}", auth=AUTH).text,
        f"/admin/sources/{source.source_id}",
    )

    assert "enabled" not in fields


def test_submitting_the_edit_form_unchanged_changes_nothing(
    client: TestClient, app_session: Session
) -> None:
    """The strongest test here: it pairs the template against the route.

    A field the template names differently from the route's parameter, or one it forgets, is
    invisible to every other test — the form still renders and the POST still redirects; the
    value just quietly reverts to a default. Round-tripping catches it.
    """
    source = make_source(
        app_session,
        name="Example Wire",
        discovery_method=DiscoveryMethod.SITEMAP,
        acquisition_tier=AcquisitionTier.EXTRACTION,
        rights_level=RightsLevel.HEADLINE_ONLY,
        jurisdiction="US",
        rate_limit_per_min=5,
    )
    app_session.commit()
    source_id = source.source_id
    action = f"/admin/sources/{source_id}"

    fields = _form(client.get(action, auth=AUTH).text, action)
    assert client.post(action, auth=AUTH, data=fields, follow_redirects=False).status_code == 303

    app_session.expire_all()
    after = sources.get(app_session, source_id)
    assert after is not None
    assert after.name == "Example Wire"
    assert after.discovery_method is DiscoveryMethod.SITEMAP
    assert after.acquisition_tier is AcquisitionTier.EXTRACTION
    assert after.rights_level is RightsLevel.HEADLINE_ONLY
    assert after.jurisdiction == "US"
    assert after.rate_limit_per_min == 5
    assert after.enabled is True
