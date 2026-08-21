"""Subclasses of ADK's shipped Firestore services.

docs/03-agent-design.md. `google-adk` is pinned at `2.7.0` and the coupling here is to
the shipped classes' *internals*, not merely to `BaseSessionService` — read the bump
checklist in that document before touching the version.

`CoachMemoryService` arrives at M7 with the learner model
(docs/09-roadmap.md#m7--learner-model-and-adaptation-1-week); M2 needs only the session
service.
"""

from __future__ import annotations

from coach.adk_firestore.session_service import (
    CoachSessionService,
    SessionLinkage,
    StoredEvent,
)

__all__ = ["CoachSessionService", "SessionLinkage", "StoredEvent"]
