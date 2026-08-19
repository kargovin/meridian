"""The admin surface's credential check (RFC §9).

HTTP Basic, because the credential is typed by a person at a browser prompt rather than sent
by a program. The username is read and ignored: this is one shared secret and not a
per-person identity, so requiring a particular name would imply an attribution the scheme
cannot make. Only the password is the credential.

Getting in the door is all this decides. Nothing downstream branches on who the caller is,
because there is nothing to branch on.
"""

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_basic = HTTPBasic(
    auto_error=False,
    scheme_name="AdminCredential",
    description="The shared admin credential. Any username; the password is the token.",
)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="the admin credential is required",
    # Without this the browser never prompts, and a person cannot supply the credential.
    headers={"WWW-Authenticate": 'Basic realm="Meridian admin"'},
)


def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    """Reject the request unless it carries the configured token.

    ``compare_digest`` rather than ``==``: a short-circuiting comparison returns faster the
    earlier it finds a mismatch, which leaks the secret one character at a time to anyone who
    can time the response.
    """
    if credentials is None:
        raise _UNAUTHORIZED
    expected: str = request.app.state.admin_token
    if not secrets.compare_digest(credentials.password, expected):
        raise _UNAUTHORIZED


RequireAdmin = Depends(require_admin)
