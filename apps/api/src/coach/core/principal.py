"""The authenticated caller.

docs/01-architecture.md:

    Authorization is a FastAPI dependency producing a `Principal(uid)`; every service
    method takes it and asserts `project.owner_uid == principal.uid`. Repositories never
    filter by owner implicitly — an explicit check is easier to audit than an implicit
    one.

Lives in `core/` rather than `api/` so that `services/` can require one without importing
FastAPI, and so that the agent tool layer (M3) can construct one for the user on whose
behalf a run executes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: How the principal was established. `dev` exists only under `ENV=local`
#: (docs/04-api-contract.md#authentication); `ws_ticket` is a socket that redeemed a
#: single-use ticket, which was itself minted from a revocation-checked ID token;
#: `agent` is a tool call inside an agent invocation, whose uid comes from the session
#: the invocation is running in (`agents/context.py`); and `system` is for background work
#: executing on a user's behalf from an OIDC-authenticated /internal/* call (M5).
#:
#: Nothing branches on the source — authorization is `owns()`, which reads the uid alone.
#: It exists so that a log line or an audit trail can say how the caller was established.
PrincipalSource = Literal["id_token", "dev", "ws_ticket", "agent", "system"]


@dataclass(frozen=True, slots=True)
class Principal:
    uid: str
    email: str | None = None
    display_name: str | None = None
    photo_url: str | None = None
    source: PrincipalSource = "id_token"

    def owns(self, owner_uid: str) -> bool:
        return owner_uid == self.uid
