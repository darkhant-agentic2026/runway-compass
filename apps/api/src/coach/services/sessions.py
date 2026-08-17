"""Session use cases — the app-owned half of an ADK-owned document.

docs/02-data-model.md: the session collection layout comes from the shipped
`FirestoreSessionService` and is not ours to choose. What *is* ours is who may read a
session, what it is linked to, and how the transcript is paged. That is this module.

**Ownership is structural here, not a filter.** A session lives at
`adk-session/{app}/users/{uid}/sessions/{sid}`, so asking for another user's session with
your own uid simply finds nothing. The `NotFound` this raises is therefore the honest
answer rather than a masked `Forbidden` — and it keeps session ids from being probed,
which is the same reasoning `ProjectService.require_owned` uses.
"""

from __future__ import annotations

from typing import Any

from coach.adk_firestore import CoachSessionService, SessionLinkage, StoredEvent
from coach.agents.runner import APP_NAME
from coach.core.errors import NotFound
from coach.core.principal import Principal
from coach.repositories.tasks import TaskRepository
from coach.services.models import SessionSummary
from coach.services.projects import ProjectService
from coach.services.tasks import TaskService
from coach.services.uploads import UploadService

#: `GET /api/sessions/{sid}/events?limit=` ceiling. A transcript page is for hydrating a
#: view, not for exporting a conversation.
MAX_EVENTS_PAGE = 200


class SessionService:
    def __init__(
        self,
        sessions: CoachSessionService,
        tasks: TaskService,
        task_repository: TaskRepository,
        projects: ProjectService,
        uploads: UploadService,
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._task_repository = task_repository
        self._projects = projects
        self._uploads = uploads

    # --- reads ---------------------------------------------------------------------

    async def require_owned(self, principal: Principal, session_id: str) -> SessionLinkage:
        linkage = await self._sessions.get_linkage(
            app_name=APP_NAME, user_id=principal.uid, session_id=session_id
        )
        if linkage is None:
            raise NotFound(f"No session {session_id!r}.")
        return linkage

    async def get(self, principal: Principal, session_id: str) -> SessionSummary:
        """`GET /api/sessions/{sid}` — metadata and linkage."""
        linkage = await self.require_owned(principal, session_id)
        return SessionSummary(
            id=linkage.session_id, project_id=linkage.project_id, task_id=linkage.task_id
        )

    async def list_events(
        self, principal: Principal, session_id: str, *, after_seq: int = 0, limit: int = 50
    ) -> list[StoredEvent]:
        """`GET /api/sessions/{sid}/events?after_seq=&limit=` — transcript hydration.

        Paged by `seq` rather than by timestamp, because only `seq` is a stable cursor
        (docs/03-agent-design.md#what-the-subclass-adds).
        """
        await self.require_owned(principal, session_id)
        return await self._sessions.list_events(
            app_name=APP_NAME,
            user_id=principal.uid,
            session_id=session_id,
            after_seq=after_seq,
            limit=min(limit, MAX_EVENTS_PAGE),
        )

    async def attachment_bytes(
        self, principal: Principal, session_id: str, seq: int, index: int
    ) -> tuple[bytes, str, str]:
        """`(data, mimeType, filename)` for one attachment on one transcript event.

        Addressed by `(session, seq, index)` rather than by artifact name or `gs://` URI,
        and that is the security design rather than a convenience: a session lives under
        the caller's own uid, so reaching *any* event through this method already proves
        ownership. A URI parameter would have to be validated, and validating a
        caller-supplied storage path is the kind of check that is one refactor away from
        being wrong.
        """
        await self.require_owned(principal, session_id)
        events = await self._sessions.list_events(
            app_name=APP_NAME,
            user_id=principal.uid,
            session_id=session_id,
            after_seq=seq - 1,
            limit=1,
        )
        event = next((stored for stored in events if stored.seq == seq), None)
        if event is None:
            raise NotFound(f"No event {seq} in session {session_id!r}.")

        uri = _attachment_uri(event.event_data, index)
        if uri is None:
            raise NotFound(f"Event {seq} has no attachment at index {index}.")
        return await self._uploads.bytes_for_artifact_uri(principal, uri)

    # --- writes --------------------------------------------------------------------

    async def get_or_create_for_task(
        self, principal: Principal, task_id: str
    ) -> SessionSummary:
        """`POST /api/tasks/{id}/session` — get-or-create, idempotent by construction.

        The task document carries a `sessionId` pointer, but the collection-group query on
        `taskId` is the authority: the pointer is a cache written after the session
        exists, so a crash between the two writes leaves a session that this method still
        finds rather than a duplicate it creates.
        """
        task = await self._tasks.resolve(principal, task_id)

        existing = await self._sessions.find_session_id_for_task(
            app_name=APP_NAME, task_id=task_id
        )
        if existing is not None:
            if task.session_id != existing:
                await self._task_repository.patch(
                    task.project_id, task_id, {"sessionId": existing}
                )
            return SessionSummary(id=existing, project_id=task.project_id, task_id=task_id)

        session = await self._sessions.create_session(
            app_name=APP_NAME,
            user_id=principal.uid,
            project_id=task.project_id,
            task_id=task_id,
        )
        await self._task_repository.patch(task.project_id, task_id, {"sessionId": session.id})
        return SessionSummary(id=session.id, project_id=task.project_id, task_id=task_id)

    async def create_intake(self, principal: Principal, project_id: str) -> SessionSummary:
        """The session `POST /api/projects` opens: a session with `taskId: null`.

        docs/04-api-contract.md. The Socratic intake *conversation* that fills it is M3
        (docs/09-roadmap.md#m3--the-coach-acts-on-the-board-15-weeks); creating the
        session here means a project made at M2 already has somewhere for that
        conversation to live, rather than needing a migration when it lands.
        """
        await self._projects.require_owned(principal, project_id)
        session = await self._sessions.create_session(
            app_name=APP_NAME, user_id=principal.uid, project_id=project_id, task_id=None
        )
        return SessionSummary(id=session.id, project_id=project_id, task_id=None)


def _attachment_uri(event_data: dict[str, Any], index: int) -> str | None:
    """The `file_uri` of the `index`-th file part of a stored event.

    `snake_case`, because that is what `append_event` writes — `Event.model_dump()`
    defaults to `by_alias=False` despite the model's camelCase aliases. Both spellings are
    accepted for the same reason `transcript.ts` accepts both: the shape belongs to a
    pinned dependency, not to us.
    """
    content = event_data.get("content")
    if not isinstance(content, dict):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None

    files = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        data = part.get("file_data") or part.get("fileData")
        if isinstance(data, dict):
            files.append(data)

    if not 0 <= index < len(files):
        return None
    uri = files[index].get("file_uri") or files[index].get("fileUri")
    return str(uri) if uri else None


__all__ = ["MAX_EVENTS_PAGE", "SessionService"]
