"""The ADK `Runner`, assembled once per process.

`Runner` binds an agent to the session, artifact, and memory services. It holds no
per-turn state, so one instance serves every turn on this process — which also means the
model client and its connection pool are warm rather than rebuilt per message.

`set_model` is the seam the streaming tests use. It is a method on the factory rather
than a monkeypatch target because the disconnect matrix needs to swap the model
*without* swapping anything else: the whole point of those tests is that the real
streaming path, the real checkpoint writer, and the real session service are exercised,
with only the token source scripted.
"""

from __future__ import annotations

from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService

from coach.agents.coach_agent import build_coach_agent
from coach.core.config import Settings
from coach.integrations.artifacts import ArtifactServiceProvider
from coach.integrations.model import build_model

#: The ADK `app_name`, which is also the `{appName}` segment of every session path
#: (docs/02-data-model.md). Changing it orphans every existing session, so it is a
#: constant rather than a setting.
APP_NAME = "coach"


class RunnerFactory:
    """Builds and caches the process's `Runner`."""

    def __init__(
        self,
        settings: Settings,
        session_service: BaseSessionService,
        artifacts: ArtifactServiceProvider,
    ) -> None:
        self._settings = settings
        self._session_service = session_service
        # Injected rather than built here, because `UploadService` writes into the same
        # store on finalize. Two instances would mean two `storage.Client`s against one
        # bucket, and — worse — two places that could disagree about which bucket that is.
        #
        # A provider rather than the service, so that constructing the factory resolves no
        # credentials. It is called in `runner()`, which is the first turn rather than
        # startup, and `Runner` gets the real instance it validates for.
        self._artifacts = artifacts
        self._model: BaseLlm | None = None
        self._runner: Runner | None = None

    def set_model(self, model: BaseLlm | None) -> None:
        """Install a model, discarding any cached runner. See the module docstring."""
        self._model = model
        self._runner = None

    def runner(self) -> Runner:
        if self._runner is None:
            model = self._model or build_model(self._settings)
            self._runner = Runner(
                app_name=APP_NAME,
                agent=build_coach_agent(model),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._runner


__all__ = ["APP_NAME", "RunnerFactory"]
