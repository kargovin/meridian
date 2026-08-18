"""Rendering failures into the locked envelope.

Every response this service can produce uses ``{"error": {...}}``. Four handlers are
needed for that to be true rather than aspirational: the deliberate failures, the request
validation FastAPI performs before a route is entered, the routing failures Starlette
raises before any of ours is reached, and anything unhandled.
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from meridian_contract.api import ErrorCode, ErrorDetail, ErrorResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class PlatformError(Exception):
    """Raise this rather than HTTPException: that one renders ``{"detail": ...}``."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        item_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = ErrorDetail(code=code, message=message, item_id=item_id)
        self.headers = headers


def _envelope(
    status_code: int, detail: ErrorDetail, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=detail).model_dump(),
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def _platform(request: Request, exc: PlatformError) -> JSONResponse:
        return _envelope(exc.status_code, exc.detail, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default answers 422 with {"detail": [...]}, which is a second error
        # shape the contract does not carry.
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        return _envelope(
            status.HTTP_400_BAD_REQUEST,
            ErrorDetail(
                code=ErrorCode.INVALID_REQUEST,
                message=f"{location}: {first.get('msg', 'invalid request')}".strip(": "),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _routing(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # An unknown path or method is raised by the router before any handler above sees
        # it, and answers {"detail": ...} unless it is caught here.
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            code = ErrorCode.NOT_FOUND
        elif exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            code = ErrorCode.INTERNAL
        else:
            code = ErrorCode.INVALID_REQUEST
        return _envelope(exc.status_code, ErrorDetail(code=code, message=str(exc.detail)))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Without this an unexpected failure answers in plain text, outside the envelope.
        # The message is deliberately fixed: an exception string can carry request content.
        log.exception("unhandled error serving %s", request.url.path)
        return _envelope(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorDetail(code=ErrorCode.INTERNAL, message="internal error"),
        )
