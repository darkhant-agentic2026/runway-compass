"""Projects (`/api/projects`)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status

from coach.api.deps import CurrentUser, Projects, Sessions, Tasks
from coach.api.idempotency import idempotency_guard
from coach.api.schemas import (
    BoardResponse,
    EffectivePrefsResponse,
    ProjectCreate,
    ProjectListResponse,
    ProjectPatch,
)
from coach.services.models import Project, ProjectStatus, SessionSummary

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    principal: CurrentUser,
    projects: Projects,
    project_status: ProjectStatus | None = Query(default=None, alias="status"),
) -> ProjectListResponse:
    return ProjectListResponse(projects=await projects.list(principal, project_status))


@router.post(
    "",
    response_model=Project,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(idempotency_guard)],
)
async def create_project(
    body: ProjectCreate, principal: CurrentUser, projects: Projects, sessions: Sessions
) -> Project:
    """`{ title, description? }` — creates project **and an intake session**.

    The intake session is a session with `taskId: null` (docs/04-api-contract.md), and
    from M3 it is the Socratic conversation the learner lands in.

    Its id is patched onto the project document by `create_intake` and copied onto the
    response here rather than re-read: the client navigates straight into the
    conversation, so a response whose `intakeSessionId` was still `null` would send it
    through `POST /api/projects/{id}/session` for a value this request already knows.
    """
    project = await projects.create(principal, title=body.title, description=body.description)
    intake = await sessions.create_intake(principal, project.id)
    return project.model_copy(update={"intake_session_id": intake.id})


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, principal: CurrentUser, projects: Projects) -> Project:
    return await projects.get(principal, project_id)


@router.patch(
    "/{project_id}", response_model=Project, dependencies=[Depends(idempotency_guard)]
)
async def patch_project(
    project_id: str, body: ProjectPatch, principal: CurrentUser, projects: Projects
) -> Project:
    return await projects.patch(
        principal,
        project_id,
        title=body.title,
        description=body.description,
        status=body.status,
        prefs=(
            body.prefs.model_dump(by_alias=True, exclude_none=True)
            if body.prefs is not None
            else None
        ),
    )


@router.delete("/{project_id}", response_model=Project)
async def archive_project(
    project_id: str, principal: CurrentUser, projects: Projects
) -> Project:
    """Soft delete. A project is never removed — it moves to `archived`."""
    return await projects.archive(principal, project_id)


@router.post("/{project_id}/session", response_model=SessionSummary)
async def open_intake_session(
    project_id: str, principal: CurrentUser, sessions: Sessions
) -> SessionSummary:
    """Get-or-create the project's intake session — the one with `taskId: null`.

    Added at M3. `POST /api/projects` creates the session but the contract has no way to
    resolve a project back to it, and every visit to the project after the one that
    created it needs exactly that. Shaped as a POST, and named like
    `POST /api/tasks/{id}/session`, because it is the same get-or-create.
    """
    return await sessions.get_or_create_intake(principal, project_id)


@router.get("/{project_id}/effective-prefs", response_model=EffectivePrefsResponse)
async def get_effective_prefs(
    project_id: str, principal: CurrentUser, projects: Projects
) -> EffectivePrefsResponse:
    """Resolved global ⊕ project preferences — one source of truth for UI and agent."""
    return EffectivePrefsResponse(
        project_id=project_id,
        effective_prefs=await projects.effective_prefs(principal, project_id),
    )


@router.get("/{project_id}/tasks", response_model=BoardResponse)
async def list_tasks(
    project_id: str,
    principal: CurrentUser,
    tasks: Tasks,
    include_completed: bool = Query(default=False),
    include_discarded: bool = Query(default=False),
    include_postponed: bool = Query(default=True),
) -> BoardResponse:
    return BoardResponse(
        tasks=await tasks.list_board(
            principal,
            project_id,
            include_completed=include_completed,
            include_discarded=include_discarded,
            include_postponed=include_postponed,
        )
    )
