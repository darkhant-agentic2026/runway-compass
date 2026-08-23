"""Errors, rendered as RFC 9457 `application/problem+json`.

docs/04-api-contract.md: "Errors follow RFC 9457 `application/problem+json`."

Service-layer code raises these directly; the API layer installs the handlers in
`coach.main`. Keeping the exception types in `core/` rather than `api/` is what lets
`services/` raise them without importing FastAPI.
"""

from __future__ import annotations

from typing import Any

PROBLEM_CONTENT_TYPE = "application/problem+json"

#: Problem `type` URIs are relative by design — they resolve against the service origin
#: and never require the docs site to be reachable to be meaningful.
_TYPE_PREFIX = "/problems/"


class CoachError(Exception):
    """Base class for errors that have a defined HTTP rendering."""

    status: int = 500
    title: str = "Internal Server Error"
    code: str = "internal-error"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        self.extra = extra
        super().__init__(self.detail)

    def to_problem(self, instance: str | None = None) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "type": f"{_TYPE_PREFIX}{self.code}",
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
        }
        if instance:
            problem["instance"] = instance
        problem.update(self.extra)
        return problem


class NotAuthenticated(CoachError):
    status = 401
    title = "Not authenticated"
    code = "not-authenticated"


class Forbidden(CoachError):
    """The caller is authenticated but does not own the resource.

    Note that `services/` generally raises :class:`NotFound` instead, so that a
    non-owner cannot probe for the existence of another user's resources. `Forbidden`
    is for cases where existence is already known to the caller.
    """

    status = 403
    title = "Forbidden"
    code = "forbidden"


class NotFound(CoachError):
    status = 404
    title = "Not found"
    code = "not-found"


class Conflict(CoachError):
    status = 409
    title = "Conflict"
    code = "conflict"


class InvalidTransition(Conflict):
    """A task state change the state machine does not allow.

    docs/02-data-model.md#task-state-machine
    """

    code = "invalid-transition"
    title = "Invalid task state transition"

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Cannot transition a task from {from_state!r} to {to_state!r}.",
            fromState=from_state,
            toState=to_state,
        )


class ValidationProblem(CoachError):
    status = 422
    title = "Unprocessable entity"
    code = "validation-error"


class RateLimited(CoachError):
    status = 429
    title = "Too many requests"
    code = "rate-limited"


class QuotaExceeded(CoachError):
    """A usage window (monthly, daily, or 4-hour points) is spent.

    docs/02-data-model.md#usage-quotas-m8-quotas. Distinct from :class:`RateLimited`: a
    rate limit bounds how *often* a request may be made; this bounds how *much* work a user
    may consume before the window resets. Raised before a turn is created, so there is
    nothing to resume — the client's only affordance is a retry once `resetAt` has passed.
    """

    status = 429
    title = "Usage quota exceeded"
    code = "quota-exceeded"

    def __init__(self, window: str, reset_at: Any) -> None:
        reset_iso = reset_at.isoformat() if hasattr(reset_at, "isoformat") else str(reset_at)
        super().__init__(
            f"Your {window} usage quota is exhausted. It resets at {reset_iso}.",
            window=window,
            resetAt=reset_iso,
        )
