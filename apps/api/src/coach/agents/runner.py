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

from coach.agents.autonomous_agent import build_autonomous_agent
from coach.agents.coach_agent import build_coach_agent
from coach.agents.prompt import PromptBuilder
from coach.agents.research_agent import build_research_agent
from coach.agents.research_tools import ResearchTools
from coach.agents.tools import DomainTools
from coach.core.app import APP_NAME
from coach.core.config import Settings
from coach.integrations.artifacts import ArtifactServiceProvider
from coach.integrations.model import build_model


class RunnerFactory:
    """Builds and caches the process's `Runner`."""

    def __init__(
        self,
        settings: Settings,
        session_service: BaseSessionService,
        artifacts: ArtifactServiceProvider,
        *,
        tools: DomainTools,
        research_tools: ResearchTools,
        prompt: PromptBuilder,
    ) -> None:
        self._settings = settings
        self._session_service = session_service
        self._research_tools = research_tools
        # Both are process-wide and stateless over the services they wrap. They are built
        # once for the same reason the `Runner` is: `LlmAgent` derives every tool's
        # declaration from the callable, and rebuilding the agent per turn would rebuild
        # (and re-cache) nine JSON schemas for no change.
        self._tools = tools
        self._prompt = prompt
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
        self._research: Runner | None = None
        self._autonomous: Runner | None = None

    def set_model(self, model: BaseLlm | None) -> None:
        """Install a model, discarding any cached runner. See the module docstring."""
        self._model = model
        self._runner = None
        self._research = None
        self._autonomous = None

    def runner(self) -> Runner:
        if self._runner is None:
            self._runner = Runner(
                app_name=APP_NAME,
                agent=build_coach_agent(
                    self._build_model(),
                    tools=self._tools.as_tools(),
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._runner

    def research_runner(self) -> Runner:
        """The `research_agent` runner, sharing everything but the agent.

        Same `app_name`, same session service, same artifact service — a research run
        writes into the task's own session, so its tool calls and its report land in the
        transcript the learner is reading (docs/05-autonomous-runs.md invariant 3). What
        differs is the agent, its instruction, and above all its tool set: `ResearchTools`
        has no board-mutating tool at all (docs/10-risks.md#r7).

        Cached separately rather than rebuilt per run, for the reason in the module
        docstring: `LlmAgent` derives a JSON schema per tool from the callable, and this
        agent also owns a `search_agent` sub-agent that would be rebuilt with it.
        """
        if self._research is None:
            self._research = Runner(
                app_name=APP_NAME,
                agent=build_research_agent(
                    self._build_model(),
                    tools=self._research_tools.as_tools(),
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._research

    def autonomous_runner(self) -> Runner:
        """The `propose_tasks` runner — the background pass over the board.

        Third runner, same everything but the agent, for the same reason `research_runner`
        is the second: the tool set is the safety rail
        (docs/03-agent-design.md#safety-rails-on-autonomy), and a rail expressed as an
        agent cannot be talked out of. `DomainTools.as_autonomous_tools()` is the
        enumeration; nothing here decides it.
        """
        if self._autonomous is None:
            self._autonomous = Runner(
                app_name=APP_NAME,
                agent=build_autonomous_agent(
                    self._build_model(),
                    tools=self._tools.as_autonomous_tools(),
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._autonomous

    def _build_model(self) -> BaseLlm:
        return self._model or build_model(self._settings)


__all__ = ["RunnerFactory"]
