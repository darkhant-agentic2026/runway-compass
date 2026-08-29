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

from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.models.base_llm import BaseLlm
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService

from coach.agents.autonomous_agent import build_autonomous_agent
from coach.agents.project_coach import build_project_coach
from coach.agents.prompt import PromptBuilder
from coach.agents.research_tools import ResearchTools
from coach.agents.research_workflow import (
    ModelThrottle,
    build_research_workflow,
    build_roadmap_workflow,
)
from coach.agents.task_teacher import build_task_teacher
from coach.agents.tools import DomainTools
from coach.core.app import APP_NAME
from coach.core.config import Settings
from coach.integrations.artifacts import ArtifactServiceProvider
from coach.integrations.model import TokenRateLimiter, build_model


class RunnerFactory:
    """Builds and caches the process's `Runner`."""

    def __init__(
        self,
        settings: Settings,
        session_service: BaseSessionService,
        artifacts: ArtifactServiceProvider,
        *,
        memory_service: BaseMemoryService | None = None,
        tools: DomainTools,
        research_tools: ResearchTools,
        research_throttle: ModelThrottle,
        token_limiter: TokenRateLimiter,
        prompt: PromptBuilder,
    ) -> None:
        self._settings = settings
        self._session_service = session_service
        self._memory_service = memory_service
        self._research_tools = research_tools
        self._research_throttle = research_throttle
        self._token_limiter = token_limiter
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
        # credentials. It is called by whichever `*_runner()` method the first turn
        # reaches, rather than at startup, and `Runner` gets the real instance it
        # validates for.
        self._artifacts = artifacts
        self._model: BaseLlm | None = None
        self._project: Runner | None = None
        self._task: Runner | None = None
        self._research: Runner | None = None
        self._roadmap: Runner | None = None
        self._autonomous: Runner | None = None

    def set_model(self, model: BaseLlm | None) -> None:
        """Install a model, discarding any cached runner. See the module docstring."""
        self._model = model
        self._project = None
        self._task = None
        self._research = None
        self._roadmap = None
        self._autonomous = None

    def project_runner(self) -> Runner:
        """The `project_coach` runner — intake, and every later board-level conversation.

        `services/turns.py` picks this one whenever the turn's session has no linked
        task (`taskId: null`), which is the property that makes this agent's "no
        item-level tool at all" true by construction rather than by prompt.
        """
        if self._project is None:
            self._project = Runner(
                app_name=APP_NAME,
                agent=build_project_coach(
                    self._build_model(),
                    tools=self._tools.as_project_tools(),
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                memory_service=self._memory_service,
                artifact_service=self._artifacts(),
            )
        return self._project

    def task_runner(self) -> Runner:
        """The `task_teacher` runner — the conversation about one task.

        `services/turns.py` picks this one whenever the turn's session is linked to a
        task. `DomainTools.as_task_tools()` has no `add_task`, which is the structural
        half of docs/09-roadmap.md#m6's fix: a learner describing extra work here cannot
        land on the board beside the task, because the tool that would do that is not in
        this agent's list.
        """
        if self._task is None:
            self._task = Runner(
                app_name=APP_NAME,
                agent=build_task_teacher(
                    self._build_model(),
                    tools=self._tools.as_task_tools(),
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                memory_service=self._memory_service,
                artifact_service=self._artifacts(),
            )
        return self._task

    def research_runner(self) -> Runner:
        """The `research_workflow` runner, sharing everything but the agent.

        Same `app_name`, same session service, same artifact service — a research run
        writes into its own dedicated session (docs/02-data-model.md, since M8), so its
        tool calls and its report land in the transcript the research view reads. What
        differs is the agent, its instructions, and above all its tool set, split since M9
        across the pipeline's nodes: no node has a board-mutating tool, and only
        `reviewer_writer` can call `post_research_report` (docs/10-risks.md#r7).

        Cached separately rather than rebuilt per run, for the reason in the module
        docstring: `LlmAgent` derives a JSON schema per tool from the callable, and this
        pipeline also owns a `search_agent` sub-agent that would be rebuilt with it.
        """
        if self._research is None:
            self._research = Runner(
                app_name=APP_NAME,
                # `Runner.agent` is annotated `BaseAgent | None`, but `Runner.run_async` has a
                # dedicated branch for a `Workflow` (a `BaseNode`, not a `BaseAgent`):
                # `isinstance(self.agent, BaseNode) and not isinstance(self.agent, BaseAgent)`
                # (docs/03-agent-design.md, "Workflow as a turn root"). The annotation has not
                # caught up with what the runtime already does.
                agent=build_research_workflow(  # type: ignore[arg-type]
                    self._build_model(),
                    research_tools=self._research_tools,
                    throttle=self._research_throttle,
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._research

    def roadmap_runner(self) -> Runner:
        """The `roadmap_workflow` runner — `research_workflow`'s taskless sibling.

        Not yet reached by `services/turns.py::_resolve_agent` for any real caller:
        `ResearchService`/`RunExecutor` still start every run, taskless or not, with
        `agent="research"`. This exists so `agent="roadmap"` is a real, cached, fully
        wired runner a test (or a later change) can start a turn against directly, the
        same way `research_runner` is — see `agents/research_workflow.py`'s module
        docstring for what "not wired in yet" means here.
        """
        if self._roadmap is None:
            self._roadmap = Runner(
                app_name=APP_NAME,
                agent=build_roadmap_workflow(  # type: ignore[arg-type]
                    self._build_model(),
                    research_tools=self._research_tools,
                    throttle=self._research_throttle,
                    before_agent_callback=self._prompt,
                ),
                session_service=self._session_service,
                artifact_service=self._artifacts(),
            )
        return self._roadmap

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
        return self._model or build_model(self._settings, self._token_limiter)


__all__ = ["RunnerFactory"]
