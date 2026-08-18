"""Caller identity.

The token is not verified here — its value is taken as the consumer name. What this
establishes is that every request carries an identity and every job records one, which is
what the job poll authorizes against.
"""

from typing import Annotated

from fastapi import Depends, Request, status
from meridian_contract.api import ErrorCode

from meridian_platform.errors import PlatformError

_SCHEME = "bearer"


def caller(request: Request) -> str:
    """The consumer named by the request's bearer token.

    ``invalid_request`` because the locked error enum has no code for a missing credential;
    the 401 is what distinguishes it from a malformed body.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")

    if scheme.lower() != _SCHEME or not token.strip():
        raise PlatformError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_REQUEST,
            "a bearer token naming the consumer is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


Consumer = Annotated[str, Depends(caller)]
