"""Rendering failures into the locked envelope."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from meridian_contract.api import ErrorCode, ErrorDetail, ErrorResponse


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


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(PlatformError)
    async def _render(request: Request, exc: PlatformError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(error=exc.detail).model_dump(),
            headers=exc.headers,
        )
