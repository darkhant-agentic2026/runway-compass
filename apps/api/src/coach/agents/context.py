"""What an agent invocation knows about *whose* board it is acting on.

An ADK `Runner` — and therefore the agent and its tools — is built once per process
(`agents/runner.py`), so a tool function cannot close over the caller. Every per-invocation
fact has to arrive through the `ToolContext` ADK hands the tool, and there are exactly two
channels for it:

- **`tool_context.user_id`** — the uid the session belongs to. Authoritative, because the
  session lives at `adk-session/{app}/users/{uid}/…` and `TurnService` passes the
  authenticated principal's uid to `run_async`. This is what the `Principal` below is
  built from, so a tool can only ever act as the user whose session it is running in.
- **`temp:` session state** — the project and task the session is linked to, written by
  the `before_agent_callback` in `agents/prompt.py`. `temp:` rather than plain keys
  because ADK trims temp deltas before persistence, so per-invocation scaffolding does not
  accumulate in the session document (docs/02-data-model.md: session `state` is a JSON
  string, so everything written to it is re-serialized on every append).

The `Principal` built here carries `source="agent"`. Nothing branches on `source` — it is
the audit label — but calling an agent tool call an `id_token` request would be a lie in
the one place a reader goes to find out who did something.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.adk.agents.context import Context

from coach.core.errors import ValidationProblem
from coach.core.principal import Principal

#: Session-state keys the callback writes and the tools read. `temp:`-prefixed, so they
#: are invocation scaffolding rather than conversation state.
PROJECT_ID_KEY = "temp:coach_project_id"
TASK_ID_KEY = "temp:coach_task_id"
DEFAULT_MINUTES_KEY = "temp:coach_default_minutes"
#: The minute budget a research report's required list has to fit inside — the task's own
#: estimate, falling back to the project default. `post_research_report` validates against
#: this rather than re-reading the number out of the rendered instruction.
RESEARCH_BUDGET_KEY = "temp:coach_research_budget"

#: Per-invocation counter behind the `add_task` cap. Also `temp:`, which is what makes the
#: cap per *run* rather than per session: docs/03-agent-design.md guards `add_task` at
#: "≤ 5/run", and a persisted counter would exhaust the budget forever after five tasks.
ADDED_TASKS_KEY = "temp:coach_added_tasks"

#: docs/03-agent-design.md, `add_task`: "≤ 5/run".
MAX_TASKS_PER_RUN = 5

#: docs/03-agent-design.md, `add_task`: "minutes <= 3x default".
MAX_TASK_MINUTES_FACTOR = 3


@dataclass(frozen=True, slots=True)
class AgentContext:
    """The caller, and the board this invocation is about."""

    principal: Principal
    project_id: str
    task_id: str | None
    default_task_minutes: int

    @property
    def max_task_minutes(self) -> int:
        return self.default_task_minutes * MAX_TASK_MINUTES_FACTOR


def agent_context(tool_context: Context) -> AgentContext:
    """Read the invocation's context out of `ToolContext`.

    Raises:
        ValidationProblem: if the session has no project linkage. A tool that mutates a
            board with no idea which board it is looking at is not a recoverable
            situation, and `tool_result` reports it as a failed call rather than
            inventing a project id.
    """
    state = tool_context.state
    project_id = state.get(PROJECT_ID_KEY)
    if not project_id:
        raise ValidationProblem(
            "This conversation is not linked to a project, so the board cannot be "
            "changed from it."
        )
    return AgentContext(
        principal=Principal(uid=tool_context.user_id, source="agent"),
        project_id=str(project_id),
        task_id=(str(state[TASK_ID_KEY]) if state.get(TASK_ID_KEY) else None),
        default_task_minutes=int(state.get(DEFAULT_MINUTES_KEY) or 45),
    )


def claim_task_slot(tool_context: Context) -> None:
    """Spend one of this run's `add_task` allowance.

    Raises:
        ValidationProblem: when the run has already added `MAX_TASKS_PER_RUN` tasks.
    """
    used = int(tool_context.state.get(ADDED_TASKS_KEY) or 0)
    if used >= MAX_TASKS_PER_RUN:
        raise ValidationProblem(
            f"This turn has already added {MAX_TASKS_PER_RUN} tasks, which is the cap. "
            "Propose the rest to the learner instead of creating them."
        )
    tool_context.state[ADDED_TASKS_KEY] = used + 1


__all__ = [
    "ADDED_TASKS_KEY",
    "DEFAULT_MINUTES_KEY",
    "MAX_TASKS_PER_RUN",
    "MAX_TASK_MINUTES_FACTOR",
    "PROJECT_ID_KEY",
    "RESEARCH_BUDGET_KEY",
    "TASK_ID_KEY",
    "AgentContext",
    "agent_context",
    "claim_task_slot",
]
