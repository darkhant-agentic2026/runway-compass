"""FastAPI dependency wiring.

Repositories and services are constructed once per process and hung off `app.state`, not
rebuilt per request: they are stateless over a shared Firestore client, and rebuilding
them would rebuild that client's gRPC channel too.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from coach.core.config import Settings
from coach.core.principal import Principal
from coach.repositories.firestore import Database
from coach.repositories.idempotency import IdempotencyRepository
from coach.repositories.projects import ProjectRepository
from coach.repositories.tasks import TaskRepository
from coach.repositories.users import UserRepository
from coach.services.projects import ProjectService
from coach.services.tasks import TaskService
from coach.services.users import UserService


class Container:
    """Everything the routers need, assembled once at startup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database.from_settings(settings)

        self.user_repository = UserRepository(self.db)
        self.project_repository = ProjectRepository(self.db)
        self.task_repository = TaskRepository(self.db)
        self.idempotency_repository = IdempotencyRepository(self.db)

        self.users = UserService(self.user_repository)
        self.projects = ProjectService(self.project_repository, self.users)
        self.tasks = TaskService(
            self.db, self.task_repository, self.project_repository, self.projects
        )


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_user_service(container: Container = Depends(get_container)) -> UserService:
    return container.users


def get_project_service(container: Container = Depends(get_container)) -> ProjectService:
    return container.projects


def get_task_service(container: Container = Depends(get_container)) -> TaskService:
    return container.tasks


Users = Annotated[UserService, Depends(get_user_service)]
Projects = Annotated[ProjectService, Depends(get_project_service)]
Tasks = Annotated[TaskService, Depends(get_task_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

# Re-exported so routers depend on one module.
from coach.api.auth import require_user  # noqa: E402

CurrentUser = Annotated[Principal, Depends(require_user)]
