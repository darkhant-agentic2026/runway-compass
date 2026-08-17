"""Request authentication.

docs/04-api-contract.md#authentication is the specification. Two things in here are
deliberate and load-bearing:

1. **`ENV=local` accepts `Authorization: Bearer dev:<uid>`.** Identity Platform has no
   local emulator, so this stands in for one. It is auth-bypass code, on purpose, and it
   is guarded by a single `settings.is_local` check that a parametrized regression test
   pins for every other `ENV` value (`tests/test_auth_local_bypass.py`). Do not delete
   that test, and do not widen the condition.

2. **`check_revoked` is `True` on exactly two endpoints.** It costs a network round-trip
   to identitytoolkit, so it is paid only where a long-lived or irreversible credential
   is at stake: `POST /api/ws-ticket` and `DELETE /api/me`. Everywhere else verification
   is an offline signature check against cached Google public keys. Also pinned by test.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, Request

from coach.core.config import Settings
from coach.core.errors import NotAuthenticated
from coach.core.principal import Principal

logger = logging.getLogger(__name__)

#: The prefix that marks a local development token. Only ever consulted when ENV=local.
DEV_TOKEN_PREFIX = "dev:"

_firebase_app: Any | None = None


def _get_firebase_app(settings: Settings) -> Any:
    """Initialise the Admin SDK once, lazily.

    Lazily because `ENV=local` never reaches this path, and initialising at import time
    would make local dev require credentials it does not have.
    """
    global _firebase_app
    if _firebase_app is None:
        import firebase_admin

        try:
            _firebase_app = firebase_admin.get_app()
        except ValueError:
            _firebase_app = firebase_admin.initialize_app(
                options={"projectId": settings.google_cloud_project}
            )
    return _firebase_app


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise NotAuthenticated("Expected an 'Authorization: Bearer <token>' header.")
    return token.strip()


def verify_id_token(settings: Settings, token: str, *, check_revoked: bool) -> Principal:
    """Verify an Identity Platform ID token and build the `Principal`.

    Signature, audience, and expiry are checked offline against cached Google public
    keys — no network call — unless `check_revoked` is set, which fetches the user
    record from identitytoolkit.
    """
    from firebase_admin import auth as firebase_auth

    try:
        claims = firebase_auth.verify_id_token(
            token, app=_get_firebase_app(settings), check_revoked=check_revoked
        )
    except Exception as exc:
        logger.info("id token rejected", extra={"reason": type(exc).__name__})
        raise NotAuthenticated("The supplied ID token is not valid.") from exc

    uid = claims.get("uid") or claims.get("sub")
    if not uid:
        raise NotAuthenticated("The supplied ID token carries no subject.")
    return Principal(
        uid=str(uid),
        email=claims.get("email"),
        display_name=claims.get("name"),
        photo_url=claims.get("picture"),
        source="id_token",
    )


class Authenticated:
    """FastAPI dependency producing a `Principal`.

    Instantiated twice at module scope below rather than parametrised per request, so
    that which endpoints pay for a revocation check is visible in the router signatures
    and testable by inspection.
    """

    def __init__(self, *, check_revoked: bool = False) -> None:
        self.check_revoked = check_revoked

    async def __call__(self, request: Request) -> Principal:
        settings: Settings = request.app.state.settings
        token = _bearer_token(request)

        if settings.is_local and token.startswith(DEV_TOKEN_PREFIX):
            # ---------------------------------------------------------------------
            # DELIBERATE LOCAL-ONLY AUTH BYPASS. docs/04-api-contract.md#authentication
            # Inert for every non-"local" ENV; `tests/test_auth_local_bypass.py`
            # asserts that, parametrized over every other value.
            # ---------------------------------------------------------------------
            uid = token[len(DEV_TOKEN_PREFIX) :].strip()
            if not uid:
                raise NotAuthenticated("A dev token must name a uid: 'dev:<uid>'.")
            return Principal(
                uid=uid,
                email=f"{uid}@localhost.dev",
                display_name=uid,
                source="dev",
            )

        return verify_id_token(settings, token, check_revoked=self.check_revoked)


#: The ordinary dependency: offline verification, no added latency.
require_user = Authenticated()

#: For `POST /api/ws-ticket` and `DELETE /api/me` only — see the module docstring.
require_user_revocation_checked = Authenticated(check_revoked=True)

CurrentUser = Depends(require_user)
