"""Project use cases.

Every entry point takes a `Principal` and asserts ownership before touching
`repositories/` — that check is the security boundary (docs/02-data-model.md#access-model).
"""

from __future__ import annotations

from typing import Any

from coach.core.errors import NotFound, ValidationProblem
from coach.core.ids import project_id as new_project_id
from coach.core.principal import Principal
from coach.repositories.projects import ProjectRepository
from coach.services.models import EffectivePrefs, Project, ProjectPrefs, ProjectStatus
from coach.services.prefs import resolve_prefs
from coach.services.users import UserService

#: Only these project preference keys may be written. A whitelist rather than a
#: pass-through because the same patch shape is exposed to the `update_project_prefs`
#: agent tool at M3 (docs/03-agent-design.md), where an unbounded patch would let the
#: model write arbitrary fields onto the project document.
WRITABLE_PREF_KEYS = frozenset(
    {
        "defaultTaskMinutes",
        "guidanceStyle",
        "researchDepth",
        "allowVideos",
        "confirmItemCompletion",
        "preferredSources",
        "avoidSources",
    }
)


class ProjectService:
    def __init__(self, projects: ProjectRepository, users: UserService) -> None:
        self._projects = projects
        self._users = users

    async def require_owned(self, principal: Principal, project_id: str) -> Project:
        """Load a project the caller owns.

        A project owned by someone else raises `NotFound`, not `Forbidden`: a
        distinguishable 403 would let any signed-in user probe for the existence of
        other people's project ids.
        """
        project = await self._projects.get(project_id)
        if project is None or not principal.owns(project.owner_uid):
            raise NotFound(f"No project {project_id!r}.")
        return project

    async def list(
        self, principal: Principal, status: ProjectStatus | None = None
    ) -> list[Project]:
        return await self._projects.list_for_owner(principal.uid, status)

    async def create(self, principal: Principal, *, title: str, goal: str = "") -> Project:
        """Create a project.

        docs/04-api-contract.md also has this endpoint create an intake session (a
        session with `taskId: null`). Sessions arrive at M2 with `CoachSessionService`;
        until then a project is created without one and the task board is driven by hand.
        """
        # Ensure the user document exists, so `globalPrefs` can be resolved against this
        # project from the moment it is created.
        await self._users.get_or_create(principal)
        project = Project(id=new_project_id(), owner_uid=principal.uid, title=title, goal=goal)
        return await self._projects.create(project)

    async def get(self, principal: Principal, project_id: str) -> Project:
        return await self.require_owned(principal, project_id)

    async def patch(
        self,
        principal: Principal,
        project_id: str,
        *,
        title: str | None = None,
        goal: str | None = None,
        status: ProjectStatus | None = None,
        prefs: dict[str, Any] | None = None,
    ) -> Project:
        project = await self.require_owned(principal, project_id)

        updates: dict[str, Any] = {}
        if title is not None:
            updates["title"] = title
        if goal is not None:
            updates["goal"] = goal
        if status is not None:
            updates["status"] = status.value
        if prefs:
            unknown = sorted(set(prefs) - WRITABLE_PREF_KEYS)
            if unknown:
                raise ValidationProblem(
                    f"Unknown project preference key(s): {', '.join(unknown)}."
                )
            # Validate the values through the model before writing them.
            ProjectPrefs.model_validate(prefs)
            for key, value in prefs.items():
                updates[f"prefs.{key}"] = value

        if not updates:
            return project
        await self._projects.patch(project_id, updates)
        return await self.require_owned(principal, project_id)

    async def archive(self, principal: Principal, project_id: str) -> Project:
        """`DELETE /api/projects/{id}` — a soft delete to `archived`."""
        return await self.patch(principal, project_id, status=ProjectStatus.ARCHIVED)

    async def effective_prefs(self, principal: Principal, project_id: str) -> EffectivePrefs:
        """`GET /api/projects/{id}/effective-prefs`.

        One source of truth for the UI, the API, and the agent's prompt builder — the
        resolution itself is the pure function in `services/prefs.py`.
        """
        project = await self.require_owned(principal, project_id)
        global_prefs = await self._users.global_prefs(principal)
        return resolve_prefs(global_prefs, project.prefs)
