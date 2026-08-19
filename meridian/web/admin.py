"""The admin surface over the source registry (FR-I2, FR-I6).

Every write is a POST because an HTML form can send nothing else, so the route path carries
the meaning the verb would in a machine API. Each one answers with a redirect rather than a
page: a POST that renders its own result is re-sent by the browser's reload button, and
re-sending an emergency disable is at best confusing.

There is no delete. ``canonical_record.source_id`` is declared ``ondelete="RESTRICT"``, so a
source with articles cannot be removed and should not be — disabling it is what stopping a
source means (FR-I6).

Routes hold no database logic; they parse a request, call ``db.sources`` and answer.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from meridian.db import sources
from meridian.db.models import Source
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

#: Where every write lands. One place, so a new write route cannot invent its own.
_LIST = "/admin/sources"


def _redirect() -> RedirectResponse:
    """303, not 302.

    303 tells the browser to follow with GET. A 302 leaves the method at the browser's
    discretion, and historically that meant re-POSTing to the list route.
    """
    return RedirectResponse(_LIST, status_code=status.HTTP_303_SEE_OTHER)


def _conflict(request: Request, action: str, submitted: dict[str, object]) -> HTMLResponse:
    """Re-render the form with what was typed, rather than answering 500 or losing it.

    Only ``home_url`` is constrained, so that is the only conflict this can be. Building an
    unsaved ``Source`` is what lets the same template redisplay the values.
    """
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {
            "source": Source(**submitted),
            "action": action,
            "error": "A source with that home URL already exists.",
            **_VOCABULARIES,
        },
        status_code=status.HTTP_409_CONFLICT,
    )


@router.get("/sources", response_class=HTMLResponse)
def list_sources(request: Request, session: Db) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "sources/list.html",
        {"sources": sources.list_all(session), **_VOCABULARIES},
    )


@router.get("/sources/new", response_class=HTMLResponse)
def new_source(request: Request) -> HTMLResponse:
    """The empty create form.

    Declared before ``/sources/{source_id}``: FastAPI matches in declaration order, and a
    typed path parameter rejects with 422 rather than falling through to a later route, so
    the reverse order makes this URL unreachable.
    """
    return templates.TemplateResponse(
        request, "sources/form.html", {"source": None, "action": _LIST, **_VOCABULARIES}
    )


@router.get("/sources/{source_id}", response_class=HTMLResponse)
def edit_source(source_id: int, request: Request, session: Db) -> HTMLResponse:
    source = sources.get(session, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    return templates.TemplateResponse(
        request,
        "sources/form.html",
        {"source": source, "action": f"{_LIST}/{source_id}", **_VOCABULARIES},
    )


@router.post("/sources")
def create_source(
    request: Request,
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    rights_level: Annotated[RightsLevel, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form()],
    enabled: Annotated[bool, Form()] = True,
) -> Response:
    submitted: dict[str, object] = {
        "name": name,
        "home_url": home_url,
        "discovery_method": discovery_method,
        "acquisition_tier": acquisition_tier,
        "rights_level": rights_level,
        "jurisdiction": jurisdiction,
        "rate_limit_per_min": rate_limit_per_min,
        "enabled": enabled,
    }
    try:
        sources.create(session, **submitted)  # type: ignore[arg-type]
    except IntegrityError:
        session.rollback()
        return _conflict(request, _LIST, submitted)
    return _redirect()


@router.post("/sources/{source_id}")
def replace_source(
    source_id: int,
    request: Request,
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    rights_level: Annotated[RightsLevel, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form()],
    enabled: Annotated[bool, Form()] = False,
) -> Response:
    """``enabled`` defaults to False because an unchecked checkbox is simply absent."""
    submitted: dict[str, object] = {
        "name": name,
        "home_url": home_url,
        "discovery_method": discovery_method,
        "acquisition_tier": acquisition_tier,
        "rights_level": rights_level,
        "jurisdiction": jurisdiction,
        "rate_limit_per_min": rate_limit_per_min,
        "enabled": enabled,
    }
    try:
        updated = sources.replace(session, source_id, **submitted)  # type: ignore[arg-type]
    except IntegrityError:
        session.rollback()
        return _conflict(request, f"{_LIST}/{source_id}", submitted)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    return _redirect()


@router.post("/sources/{source_id}/enable")
def set_enabled(
    source_id: int,
    session: Db,
    enabled: Annotated[bool, Form()],
) -> RedirectResponse:
    """Stop or resume one source (FR-I6).

    Its own route, not a variant of the full-row write, so that stopping ingestion cannot be
    rejected because an unrelated field on the row is incomplete.
    """
    if sources.set_enabled(session, source_id, value=enabled) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    return _redirect()


@router.post("/sources/{source_id}/tier")
def set_tier(
    source_id: int,
    session: Db,
    acquisition_tier: Annotated[AcquisitionTier, Form()],
) -> RedirectResponse:
    if sources.set_acquisition_tier(session, source_id, tier=acquisition_tier) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    return _redirect()
