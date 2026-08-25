"""The admin surface over the source registry and the runtime config plane (FR-I2, FR-I6, RFC §9).

Every write is a POST because an HTML form can send nothing else, so the route path carries
the meaning the verb would in a machine API. Each one answers with a redirect rather than a
page: a POST that renders its own result is re-sent by the browser's reload button, and
re-sending an emergency disable is at best confusing.

A publisher cannot be deleted — ``canonical_record.source_id`` is ``ondelete="RESTRICT"``, so
one with articles cannot be removed and should not be; disabling is what stopping a publisher
means (FR-I6). A feed can be, because a feed is a URL we poll and a retired one is not history.

Governing fields — the ones whose stale value causes harm rather than inconvenience — each get
their own single-field route and carry the row's ``updated_at``. Publishers: ``enabled``,
``permitted_to_ingest``, ``rights_level``. Feeds: ``enabled``, ``acquisition_tier``.

Routes hold no database logic; they parse a request, call ``db.sources`` or ``db.feeds``, and
answer.
"""

import datetime as dt
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db import feeds, poll_state, runtime_config, sources
from meridian.web.auth import RequireAdmin

router = APIRouter(prefix="/admin", dependencies=[RequireAdmin])

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

#: The vocabularies the form offers. Read from the contract rather than repeated as literals
#: in the markup, so a member added there appears in the form and cannot fall out of step
#: with the CHECK constraint built from the same enum.
_VOCABULARIES: dict[str, object] = {
    "discovery_methods": list(DiscoveryMethod),
    "tiers": list(AcquisitionTier),
    "rights_levels": list(RightsLevel),
}


def db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session
        session.commit()


Db = Annotated[Session, Depends(db)]

#: Where a publisher write lands. One place, so a new write route cannot invent its own.
_LIST = "/admin/sources"


def _stale(exc: sources.StaleWrite) -> HTTPException:
    """A write offered by a page that no longer reflects the row.

    409 rather than a silent overwrite: the value this control carries was rendered before the
    row changed, so applying it would revert whatever changed it — which on this surface means
    reverting a rights revocation or a stop-ingestion instruction.
    """
    return HTTPException(status.HTTP_409_CONFLICT, f"{exc} — reload and try again")


def _redirect(path: str = _LIST) -> RedirectResponse:
    """303, not 302.

    303 tells the browser to follow with GET. A 302 leaves the method at the browser's
    discretion, and historically that meant re-POSTing to the route just written.
    """
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _missing(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"no such {what}")


# --------------------------------------------------------------------------- publishers


@router.get("/sources", response_class=HTMLResponse)
def list_sources(request: Request, session: Db) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sources/list.html",
        {
            "sources": sources.list_all(session),
            "feed_counts": feeds.counts_by_source(session),
            **_VOCABULARIES,
        },
    )


@router.get("/sources/new", response_class=HTMLResponse)
def new_source(request: Request) -> HTMLResponse:
    """The empty create form.

    Declared before ``/sources/{source_id}``: FastAPI matches in declaration order, and a
    typed path parameter rejects with 422 rather than falling through to a later route, so
    the reverse order makes this URL unreachable.
    """
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {"source": None, "feeds": [], "poll_states": {}, **_VOCABULARIES},
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def edit_source(source_id: int, request: Request, session: Db) -> HTMLResponse:
    """The publisher's descriptive fields, and its feeds.

    Feeds live here rather than on a page of their own because a feed is meaningless without
    the publisher whose rights and rate limit govern it — and because the question an operator
    arrives with is "what are we polling for this outlet", not "show me feed 12".
    """
    source = sources.get(session, source_id)
    if source is None:
        raise _missing("source")
    owned = feeds.for_source(session, source_id)
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {
            "source": source,
            "feeds": owned,
            # Read as a mapping rather than a relationship: a lazy one would load after the
            # request's session closed, and an eager one would put a join on the publisher
            # list that does not want it. Same reasoning as feeds.counts_by_source.
            "poll_states": poll_state.for_feeds(session, [f.feed_id for f in owned]),
            **_VOCABULARIES,
        },
    )


@router.post("/sources")
def create_source(
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    rights_level: Annotated[RightsLevel, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form(gt=0)],
    user_agent: Annotated[str, Form()] = "",
    enabled: Annotated[bool, Form()] = True,
    permitted_to_ingest: Annotated[bool, Form()] = True,
) -> RedirectResponse:
    source = sources.create(
        session,
        name=name,
        home_url=home_url,
        rights_level=rights_level,
        jurisdiction=jurisdiction,
        rate_limit_per_min=rate_limit_per_min,
        # An empty field means "no override", which is NULL rather than the empty string: a
        # blank User-Agent header is not the same request as an absent one.
        user_agent=user_agent or None,
        enabled=enabled,
        permitted_to_ingest=permitted_to_ingest,
    )
    # To the publisher's own page, not the list: a publisher with no feeds polls nothing, and
    # adding one is the next thing to do.
    return _redirect(f"{_LIST}/{source.source_id}")


@router.post("/sources/{source_id}")
def describe_source(
    source_id: int,
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form(gt=0)],
    user_agent: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """The descriptive fields only.

    ``enabled``, ``permitted_to_ingest`` and ``rights_level`` each have their own route below,
    so that a form rendered before an emergency change cannot revert it on submit.
    """
    if (
        sources.describe(
            session,
            source_id,
            name=name,
            home_url=home_url,
            jurisdiction=jurisdiction,
            rate_limit_per_min=rate_limit_per_min,
            user_agent=user_agent or None,
        )
        is None
    ):
        raise _missing("source")
    return _redirect()


@router.post("/sources/{source_id}/enable")
def set_enabled(
    source_id: int,
    session: Db,
    enabled: Annotated[bool, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    """Stop or resume one publisher (FR-I6).

    Its own route, not a variant of the full-row write, so that stopping ingestion cannot be
    rejected because an unrelated field on the row is incomplete.
    """
    try:
        if (
            sources.set_enabled(
                session, source_id, value=enabled, expected_updated_at=expected_updated_at
            )
            is None
        ):
            raise _missing("source")
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    return _redirect()


@router.post("/sources/{source_id}/rights")
def set_rights(
    source_id: int,
    session: Db,
    rights_level: Annotated[RightsLevel, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    """Grant or revoke body-text rights (FR-S5). Applies to articles already ingested."""
    try:
        if (
            sources.set_rights_level(
                session, source_id, level=rights_level, expected_updated_at=expected_updated_at
            )
            is None
        ):
            raise _missing("source")
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    return _redirect()


@router.post("/sources/{source_id}/permitted")
def set_permitted(
    source_id: int,
    session: Db,
    permitted_to_ingest: Annotated[bool, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    """Record whether we may ingest this publisher at all.

    Separate from both the rights level and the enable toggle: a terms exclusion is not a lower
    rung on the rights ladder, and it is not the operational switch that gets flipped back when
    an incident passes.
    """
    try:
        if (
            sources.set_permitted_to_ingest(
                session,
                source_id,
                value=permitted_to_ingest,
                expected_updated_at=expected_updated_at,
            )
            is None
        ):
            raise _missing("source")
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    return _redirect()


# --------------------------------------------------------------------------- feeds


@router.post("/sources/{source_id}/feeds")
def create_feed(
    source_id: int,
    session: Db,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    enabled: Annotated[bool, Form()] = True,
) -> RedirectResponse:
    if sources.get(session, source_id) is None:
        raise _missing("source")
    feeds.create(
        session,
        source_id=source_id,
        name=name,
        url=url,
        discovery_method=discovery_method,
        acquisition_tier=acquisition_tier,
        enabled=enabled,
    )
    return _redirect(f"{_LIST}/{source_id}")


def _feed_or_404(session: Session, feed_id: int) -> int:
    """Resolve a feed to the publisher page its write should return to."""
    feed = feeds.get(session, feed_id)
    if feed is None:
        raise _missing("feed")
    return feed.source_id


@router.post("/feeds/{feed_id}")
def describe_feed(
    feed_id: int,
    session: Db,
    name: Annotated[str, Form()],
    url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
) -> RedirectResponse:
    """The descriptive fields only — ``enabled`` and ``acquisition_tier`` have their own routes."""
    source_id = _feed_or_404(session, feed_id)
    feeds.describe(session, feed_id, name=name, url=url, discovery_method=discovery_method)
    return _redirect(f"{_LIST}/{source_id}")


@router.post("/feeds/{feed_id}/enable")
def set_feed_enabled(
    feed_id: int,
    session: Db,
    enabled: Annotated[bool, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    """Stop or resume polling one feed — maintenance, not the Legal stop."""
    source_id = _feed_or_404(session, feed_id)
    try:
        feeds.set_enabled(session, feed_id, value=enabled, expected_updated_at=expected_updated_at)
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    return _redirect(f"{_LIST}/{source_id}")


@router.post("/feeds/{feed_id}/tier")
def set_feed_tier(
    feed_id: int,
    session: Db,
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    source_id = _feed_or_404(session, feed_id)
    try:
        feeds.set_acquisition_tier(
            session, feed_id, tier=acquisition_tier, expected_updated_at=expected_updated_at
        )
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    return _redirect(f"{_LIST}/{source_id}")


@router.post("/feeds/{feed_id}/delete")
def delete_feed(feed_id: int, session: Db) -> RedirectResponse:
    """Remove a feed. Records it discovered survive with their ``feed_id`` set to NULL."""
    source_id = _feed_or_404(session, feed_id)
    feeds.delete(session, feed_id)
    return _redirect(f"{_LIST}/{source_id}")


# --------------------------------------------------------------------------- runtime config


_CONFIG = "/admin/config"

#: Declared knobs by key. A write names a key, and only a declared one is writable — the table
#: is a config plane, not a place to store arbitrary rows through the web.
_KNOBS = {knob.key: knob for knob in runtime_config.KNOBS}


@router.get("/config", response_class=HTMLResponse)
def list_config(request: Request, session: Db) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "config/list.html",
        {"states": runtime_config.states(session)},
    )


@router.post("/config/{key}")
def set_config(
    key: str,
    session: Db,
    value: Annotated[int, Form()],
    expected_updated_at: Annotated[dt.datetime, Form()],
) -> RedirectResponse:
    """Write one knob.

    Compare-and-set like the registry's governing fields, and for the same reason rather than by
    analogy: the cadence is the primary freshness lever, so a page rendered before a change and
    submitted after it would revert that change with a 303 and no error — and a freshness
    regression is the kind nobody notices, because nothing fails.
    """
    knob = _KNOBS.get(key)
    if knob is None:
        raise _missing("knob")
    try:
        written = runtime_config.set_int(
            session, knob, value=value, expected_updated_at=expected_updated_at
        )
    except sources.StaleWrite as stale:
        raise _stale(stale) from stale
    except ValueError as bad:
        # The form carries min/max, so reaching this means the bounds were bypassed rather
        # than mistyped — a hand-made request, or a browser that ignored them.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(bad)) from bad
    if written is None:
        raise _missing("knob")
    return _redirect(_CONFIG)
