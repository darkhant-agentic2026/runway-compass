"""The ADK application name.

It is also the `{appName}` segment of every session, event, and state path
(docs/02-data-model.md), so changing it orphans every existing session. A constant rather
than a setting, for exactly that reason.

**It lives in `core/` rather than beside the `Runner` because both layers need it.**
`agents/runner.py` passes it to `Runner`; `services/sessions.py` passes it to every
session-service call. With the constant in `agents/`, `services/` had to import from
`agents/` — an inversion of the layering in docs/01-architecture.md that went unnoticed
until `agents/` grew a module importing `services/` and the two became a cycle.
"""

from __future__ import annotations

APP_NAME = "coach"

__all__ = ["APP_NAME"]
