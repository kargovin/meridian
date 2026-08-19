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
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from meridian_contract import AcquisitionTier, DiscoveryMethod, RightsLevel
from sqlalchemy.orm import Session

from meridian.db import sources
from meridian.web.auth import RequireAdmin

router = APIRouter(prefix="/admin", dependencies=[RequireAdmin])


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


@router.get("/sources")
def list_sources(session: Db) -> dict[str, object]:
    return {"sources": [s.name for s in sources.list_all(session)]}


@router.get("/sources/new")
def new_source() -> dict[str, object]:
    """The empty create form.

    Declared before ``/sources/{source_id}``: FastAPI matches in declaration order, and a
    typed path parameter rejects with 422 rather than falling through to a later route, so
    the reverse order makes this URL unreachable.
    """
    return {"source": None}


@router.get("/sources/{source_id}")
def edit_source(source_id: int, session: Db) -> dict[str, object]:
    source = sources.get(session, source_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such source")
    return {"source": source.name}


@router.post("/sources")
def create_source(
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    rights_level: Annotated[RightsLevel, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form()],
    enabled: Annotated[bool, Form()] = True,
) -> RedirectResponse:
    sources.create(
        session,
        name=name,
        home_url=home_url,
        discovery_method=discovery_method,
        acquisition_tier=acquisition_tier,
        rights_level=rights_level,
        jurisdiction=jurisdiction,
        rate_limit_per_min=rate_limit_per_min,
        enabled=enabled,
    )
    return _redirect()


@router.post("/sources/{source_id}")
def replace_source(
    source_id: int,
    session: Db,
    name: Annotated[str, Form()],
    home_url: Annotated[str, Form()],
    discovery_method: Annotated[DiscoveryMethod, Form()],
    acquisition_tier: Annotated[AcquisitionTier, Form()],
    rights_level: Annotated[RightsLevel, Form()],
    jurisdiction: Annotated[str, Form()],
    rate_limit_per_min: Annotated[int, Form()],
    enabled: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    """``enabled`` defaults to False because an unchecked checkbox is simply absent."""
    updated = sources.replace(
        session,
        source_id,
        name=name,
        home_url=home_url,
        discovery_method=discovery_method,
        acquisition_tier=acquisition_tier,
        rights_level=rights_level,
        jurisdiction=jurisdiction,
        rate_limit_per_min=rate_limit_per_min,
        enabled=enabled,
    )
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
