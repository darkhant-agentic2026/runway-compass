"""FastAPI dependency wiring.

Repositories and services are constructed once per process and hung off `app.state`, not
rebuilt per request: they are stateless over a shared Firestore client, and rebuilding
them would rebuild that client's gRPC channel too.

From M2 the container also owns three things that are *not* stateless and must therefore
be process-wide rather than per-request: the `TurnRegistry` holding in-flight generation
tasks, the `StreamBroker` fanning their output out, and the `RunnerFactory`'s warm model
client. A per-request container would drop generation tasks on the floor the moment the
request that started them returned — which is the exact failure
docs/04-api-contract.md#surviving-client-disconnects exists to prevent.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING, Annotated, cast

from fastapi import Depends, Request

from coach.adk_firestore import CoachSessionService
from coach.agents.prompt import PromptBuilder
from coach.agents.runner import RunnerFactory
from coach.agents.tools import DomainTools
from coach.core.config import Settings
from coach.core.principal import Principal
from coach.integrations.artifacts import artifact_service_provider
from coach.integrations.storage import build_object_store
from coach.repositories.firestore import Database, LazyAsyncClient
from coach.repositories.idempotency import IdempotencyRepository
from coach.repositories.presence import PresenceRepository
from coach.repositories.projects import ProjectRepository
from coach.repositories.tasks import TaskRepository
from coach.repositories.tickets import TicketRepository
from coach.repositories.turns import TurnRepository
from coach.repositories.uploads import UploadRepository
from coach.repositories.users import UserRepository
from coach.services.projects import ProjectService
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService
from coach.services.turns import TurnService
from coach.services.uploads import UploadService
from coach.services.users import UserService
from coach.ws.broker import StreamBroker
from coach.ws.hub import BoardUpdateHub
from coach.ws.registry import TurnRegistry

if TYPE_CHECKING:
    from google.cloud.firestore import AsyncClient


def instance_id() -> str:
    """Which process this is, for `turns/{turnId}.instanceId`.

    Cloud Run does not expose a stable per-instance identifier as an environment
    variable, so this is a per-process uuid seeded from `K_REVISION` when it is
    available. It only has to answer one question — "is the generation task here?" — and
    a process-scoped value answers it exactly, since the registry it is compared against
    is process-scoped too.
    """
    revision = os.environ.get("K_REVISION", "local")
    return f"{revision}-{uuid.uuid4().hex[:12]}"


class Container:
    """Everything the routers need, assembled once at startup."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database.from_settings(settings)
        self.instance_id = instance_id()

        self.user_repository = UserRepository(self.db)
        self.project_repository = ProjectRepository(self.db)
        self.task_repository = TaskRepository(self.db)
        self.idempotency_repository = IdempotencyRepository(self.db)
        self.turn_repository = TurnRepository(self.db)
        self.upload_repository = UploadRepository(self.db)
        self.presence_repository = PresenceRepository(self.db)
        self.tickets = TicketRepository(self.db)

        self.users = UserService(self.user_repository)
        self.projects = ProjectService(self.project_repository, self.users)
        self.tasks = TaskService(
            self.db, self.task_repository, self.project_repository, self.projects
        )

        # The ADK session service shares the process's Firestore client rather than
        # building its own: two clients would mean two gRPC channels to the same
        # database, and the emulator wiring (`FIRESTORE_EMULATOR_HOST`) is read at client
        # construction, so a second one is also a second thing to configure.
        self.session_service = CoachSessionService(
            # Lazy, so that assembling the container resolves no credentials — see
            # `LazyAsyncClient`. Passing `get_client(settings)` here is the obvious thing
            # and makes `import coach.main` require credentials, which fails in CI at
            # collection time and passes locally only because the emulator host is
            # already exported.
            # `cast` because ADK's constructor is annotated for a real client; the proxy
            # forwards every attribute it uses, and typing that faithfully would mean
            # restating `AsyncClient`'s surface for no benefit.
            client=cast("AsyncClient", LazyAsyncClient(settings)),
            root_collection=settings.adk_firestore_root_collection,
        )
        # One artifact service for the process. `UploadService` writes user uploads into
        # it on finalize and the agent reads them back through the `Runner`, so a second
        # instance would be a second client against the same bucket and a second place to
        # get the bucket name wrong.
        #
        # A callable, not the service: building the GCS-backed one resolves credentials,
        # and it is handed to ADK, which type-checks it. `artifact_service_provider`
        # explains why those two facts cannot both be satisfied by a proxy.
        self.artifacts = artifact_service_provider(settings)
        # Held on the container as well as passed to the service, because the local-only
        # PUT receiver writes into this exact instance (`api/routers/local_storage.py`).
        self.object_store = build_object_store(settings)
        self.uploads = UploadService(self.upload_repository, self.object_store, self.artifacts)

        # After `uploads`: serving an attachment's bytes for a preview goes through it.
        self.sessions = SessionService(
            self.session_service,
            self.tasks,
            self.task_repository,
            self.projects,
            self.project_repository,
            self.uploads,
        )

        self.registry = TurnRegistry()
        self.broker = StreamBroker()
        # From M3 the agent changes the board, so the board has to be told. The hub is a
        # second fan-out beside the broker because its keyspace is the *user*, not the
        # turn: a board update matters to every tab this user has open, including the one
        # that is not watching the conversation (`ws/hub.py`).
        self.board_updates = BoardUpdateHub()
        # Held on the container as well as handed to the factory: the agent-tool tests
        # call them directly, which is how a guard gets a test that does not depend on
        # persuading a model to trip it.
        self.domain_tools = DomainTools(self.tasks, self.projects, self.board_updates)
        self.prompt_builder = PromptBuilder(
            self.sessions, self.projects, self.tasks, self.users
        )
        self.runners = RunnerFactory(
            settings,
            self.session_service,
            self.artifacts,
            tools=self.domain_tools,
            prompt=self.prompt_builder,
        )
        self.turns = TurnService(
            settings,
            self.turn_repository,
            self.sessions,
            self.uploads,
            self.runners,
            self.registry,
            self.broker,
            instance_id=self.instance_id,
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


def get_session_service(container: Container = Depends(get_container)) -> SessionService:
    return container.sessions


def get_turn_service(container: Container = Depends(get_container)) -> TurnService:
    return container.turns


def get_upload_service(container: Container = Depends(get_container)) -> UploadService:
    return container.uploads


Users = Annotated[UserService, Depends(get_user_service)]
Projects = Annotated[ProjectService, Depends(get_project_service)]
Tasks = Annotated[TaskService, Depends(get_task_service)]
Sessions = Annotated[SessionService, Depends(get_session_service)]
Turns = Annotated[TurnService, Depends(get_turn_service)]
Uploads = Annotated[UploadService, Depends(get_upload_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

# Re-exported so routers depend on one module.
from coach.api.auth import require_user  # noqa: E402

CurrentUser = Annotated[Principal, Depends(require_user)]
