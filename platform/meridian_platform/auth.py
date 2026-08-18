"""Caller identity.

The token is not verified here — its value is taken as the consumer name. What this
establishes is that every request carries an identity and every job records one, which is
what the job poll authorizes against.

Declared with ``HTTPBearer`` rather than by reading the header directly so the requirement
appears in the generated document: a client generated from a contract with no security
scheme sends no credential and is rejected on every call.
"""

from typing import Annotated

from fastapi import Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from meridian_contract.api import ErrorCode

from meridian_platform.errors import PlatformError

#: ``auto_error=False``: FastAPI's own 403 body is not the locked error envelope.
_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ServiceToken",
    description="Internal service-to-service token naming the calling consumer.",
)


def caller(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    """The consumer named by the request's bearer token.

    ``invalid_request`` because the locked error enum has no code for a missing credential;
    the 401 is what distinguishes it from a malformed body.
    """
    if credentials is None or not credentials.credentials.strip():
        raise PlatformError(
            status.HTTP_401_UNAUTHORIZED,
            ErrorCode.INVALID_REQUEST,
            "a bearer token naming the consumer is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials.strip()


Consumer = Annotated[str, Depends(caller)]
