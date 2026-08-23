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
from coach.core.app import APP_NAME
from coach.core.errors import NotFound
from coach.core.principal import Principal
from coach.repositories.projects import ProjectRepository
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
        project_repository: ProjectRepository,
        uploads: UploadService,
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._task_repository = task_repository
        self._projects = projects
        self._project_repository = project_repository
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

    async def list_attachments(
        self, principal: Principal, session_id: str
    ) -> list[dict[str, str]]:
        """Every distinct file the user has sent in this conversation, oldest first.

        Read-only, and read from the transcript rather than from a second index: an
        attachment's `gs://` URI already lives on the stored event that sent it
        (`TurnService._build_content`), so this is a scan of what is already there, not a
        new record of it. Used to carry a task's (or a project's intake conversation's)
        own uploads into a research turn automatically, so the research agent can read a
        file the task's description or conversation mentions without the learner having
        to re-attach it to the research request itself
        (docs/03-agent-design.md#research_agent).

        Pages through the whole session rather than the transcript's usual one-screen
        limit — an upload from early in a long conversation is exactly the kind research
        would otherwise silently miss.
        """
        await self.require_owned(principal, session_id)
        attachments: dict[str, dict[str, str]] = {}
        cursor = 0
        while True:
            page = await self._sessions.list_events(
                app_name=APP_NAME,
                user_id=principal.uid,
                session_id=session_id,
                after_seq=cursor,
                limit=MAX_EVENTS_PAGE,
            )
            if not page:
                break
            for stored in page:
                if stored.event_data.get("author") != "user":
                    continue
                for attachment in _attachments_of(stored.event_data):
                    # First mention wins the display name; a re-send of the same file
                    # later in the conversation is the same upload, not a new one.
                    attachments.setdefault(attachment["uri"], attachment)
            cursor = page[-1].seq
            if len(page) < MAX_EVENTS_PAGE:
                break
        return list(attachments.values())

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

    async def create_research_session(
        self,
        principal: Principal,
        *,
        project_id: str,
        run_id: str,
        task_id: str | None = None,
    ) -> SessionSummary:
        """A fresh session for one research run — never get-or-create, never reused.

        docs/02-data-model.md#sessions--events-adk-owned-layout. `task_id` is the run's
        *parent* task, or `None` for research kicked off from the project coach's own
        conversation about the project as a whole. Called once per run by
        `ResearchService` and `RunExecutor`, never looked up again by anything other than
        the run that owns it (`autonomous_runs/{id}.sessionId`).
        """
        session = await self._sessions.create_research_session(
            app_name=APP_NAME,
            user_id=principal.uid,
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
        )
        return SessionSummary(id=session.id, project_id=project_id, task_id=task_id)

    async def create_intake(self, principal: Principal, project_id: str) -> SessionSummary:
        """The session `POST /api/projects` opens: a session with `taskId: null`.

        docs/04-api-contract.md. The Socratic intake conversation that fills it is M3;
        creating it at project creation means the conversation has somewhere to live from
        the first moment, and `project.intakeSessionId` is written here so that finding it
        again later is a field read rather than a query.
        """
        await self._projects.require_owned(principal, project_id)
        session = await self._sessions.create_session(
            app_name=APP_NAME, user_id=principal.uid, project_id=project_id, task_id=None
        )
        await self._project_repository.patch(project_id, {"intakeSessionId": session.id})
        return SessionSummary(id=session.id, project_id=project_id, task_id=None)

    async def get_or_create_intake(
        self, principal: Principal, project_id: str
    ) -> SessionSummary:
        """`POST /api/projects/{id}/session` — the project's intake conversation.

        Added at M3. docs/04-api-contract.md has `POST /api/projects` *create* the intake
        session but nothing that resolves a project back to it, and the workspace needs
        that on every later visit: the id is not in the creation response the client
        cached, and a second visit is a fresh page load.

        Three sources, in order of cost: the pointer on the project document; the
        collection-group scan, for projects created before the pointer existed; and
        creation, for a project whose intake session was never made. The pointer is
        repaired whenever one of the later two answers, so the scan happens once per
        legacy project rather than once per visit.
        """
        project = await self._projects.require_owned(principal, project_id)
        if project.intake_session_id is not None:
            linkage = await self._sessions.get_linkage(
                app_name=APP_NAME, user_id=principal.uid, session_id=project.intake_session_id
            )
            if linkage is not None:
                return SessionSummary(
                    id=linkage.session_id, project_id=project_id, task_id=None
                )

        existing = await self._sessions.find_intake_session_id(
            app_name=APP_NAME, project_id=project_id
        )
        if existing is not None:
            await self._project_repository.patch(project_id, {"intakeSessionId": existing})
            return SessionSummary(id=existing, project_id=project_id, task_id=None)

        return await self.create_intake(principal, project_id)


def _file_parts(event_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Every `file_data` part of a stored event, in order.

    `snake_case`, because that is what `append_event` writes — `Event.model_dump()`
    defaults to `by_alias=False` despite the model's camelCase aliases. Both spellings are
    accepted for the same reason `transcript.ts` accepts both: the shape belongs to a
    pinned dependency, not to us.
    """
    content = event_data.get("content")
    if not isinstance(content, dict):
        return []
    parts = content.get("parts")
    if not isinstance(parts, list):
        return []

    files = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        data = part.get("file_data") or part.get("fileData")
        if isinstance(data, dict):
            files.append(data)
    return files


def _attachment_uri(event_data: dict[str, Any], index: int) -> str | None:
    """The `file_uri` of the `index`-th file part of a stored event."""
    files = _file_parts(event_data)
    if not 0 <= index < len(files):
        return None
    uri = files[index].get("file_uri") or files[index].get("fileUri")
    return str(uri) if uri else None


def _attachments_of(event_data: dict[str, Any]) -> list[dict[str, str]]:
    """`{uri, mimeType, displayName}` for every file part of a stored event that has a
    URI. Skips a part with no URI rather than raising — this feeds an automatic carry-
    forward into a new turn, and a malformed one part must not cost the rest."""
    attachments = []
    for file_data in _file_parts(event_data):
        uri = file_data.get("file_uri") or file_data.get("fileUri")
        if not uri:
            continue
        mime_type = file_data.get("mime_type") or file_data.get("mimeType") or ""
        display_name = file_data.get("display_name") or file_data.get("displayName") or ""
        attachments.append(
            {"uri": str(uri), "mimeType": str(mime_type), "displayName": str(display_name)}
        )
    return attachments


__all__ = ["MAX_EVENTS_PAGE", "SessionService"]
