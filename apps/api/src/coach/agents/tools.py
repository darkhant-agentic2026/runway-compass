"""The domain tools — the coach's hands on the board.

docs/01-architecture.md states the layering rule these exist to satisfy:

> **Agent tools call `services/`, never `repositories/` directly.** […] A tool that wants
> to complete a task calls the same `TaskService.complete_task()` the user's button calls.

So there is no logic here beyond argument shaping, the guards from
docs/03-agent-design.md#domain-tools, and the `board_update` push. Every invariant — the
state machine, the single-`current` rule, rollups, fractional ordering — belongs to
`TaskService` and is reached by the tool exactly as the REST router reaches it.

Three things are deliberate and would be easy to "simplify" away:

- **A failed tool returns a result; it does not raise.** An exception out of a tool aborts
  the invocation, so a model that asked for a 9-hour task would end the turn instead of
  being told the number is too big and trying again. Every guard therefore answers
  `{"ok": false, "error": …}`, which is a fact the model can act on. Bugs — anything that
  is not a `CoachError` — still propagate, because those are ours and should be loud.
- **Results are compact and structured, never prose.** docs/03-agent-design.md: "Every tool
  returns a compact structured result (not prose) so the model reasons over facts." Ids
  are included because the next tool call needs them.
- **`discard_task` is gated by ADK's `require_confirmation`.** docs/03-agent-design.md
  marks it "**requires user confirmation** in interactive mode", and ADK 2.7 implements
  exactly that handshake: the tool call becomes an `adk_request_confirmation` function
  call, the invocation ends, and the tool body runs only once the user answers. Doing it
  with a flag rather than with a "propose" tool means the model does not need to know that
  a confirmation happened — which is the difference between a gate and an honour system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext

from coach.agents.context import (
    AgentContext,
    agent_context,
    claim_task_slot,
)
from coach.core.errors import CoachError, ValidationProblem
from coach.services.models import Origin, Task, TaskState, TaskWithSubtasks
from coach.services.projects import ProjectService
from coach.services.tasks import MAX_SPLIT_SUBTASKS, MIN_SPLIT_SUBTASKS, TaskService
from coach.ws.hub import BoardUpdateHub

logger = logging.getLogger(__name__)


def task_view(task: Task) -> dict[str, Any]:
    """What the model sees of a task.

    A projection rather than the whole document: `order` is an opaque fractional index it
    must not reason about (asking it to would invite invented keys), and timestamps are
    noise in a prompt that is already carrying the board twice.
    """
    view: dict[str, Any] = {
        "taskId": task.id,
        "title": task.title,
        "state": task.state.value,
        "estimatedMinutes": task.estimated_minutes,
        "origin": task.origin.value,
    }
    if task.parent_task_id is not None:
        view["parentTaskId"] = task.parent_task_id
    if task.rollup is not None and task.rollup.subtask_count:
        view["subtaskCount"] = task.rollup.subtask_count
        view["subtaskMinutes"] = task.rollup.total_estimated_minutes
    return view


def board_view(board: list[TaskWithSubtasks]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for parent in board:
        row = task_view(parent)
        if parent.subtasks:
            row["subtasks"] = [task_view(child) for child in parent.subtasks]
        rows.append(row)
    return rows


class DomainTools:
    """The tool catalogue, bound to the process's services.

    A class rather than closures because ADK derives each tool's declaration from the
    callable's signature: a bound method has the annotations and the docstring right there,
    and `FunctionTool` names the tool after the method. The docstrings below are therefore
    *prompt text* — they are what the model reads to decide whether to call the tool — and
    should be edited as such.
    """

    def __init__(
        self,
        tasks: TaskService,
        projects: ProjectService,
        hub: BoardUpdateHub,
    ) -> None:
        self._tasks = tasks
        self._projects = projects
        self._hub = hub

    # --- fan-out -----------------------------------------------------------------------

    async def _announce(self, context: AgentContext, task_ids: list[str]) -> None:
        await self._hub.publish(
            context.principal.uid,
            project_id=context.project_id,
            task_ids=task_ids,
            origin="agent",
        )

    # --- tools -------------------------------------------------------------------------

    async def list_tasks(
        self, include_completed: bool, tool_context: ToolContext
    ) -> dict[str, Any]:
        """List the tasks on this project's board, parents with their subtasks nested.

        Call this before proposing changes so that you are reasoning about the board as it
        is now rather than as it was earlier in the conversation.

        Args:
            include_completed: Whether to include tasks the learner has already finished.
        """
        return await self._guarded(tool_context, self._list_tasks, include_completed)

    async def _list_tasks(
        self, context: AgentContext, include_completed: bool
    ) -> dict[str, Any]:
        board = await self._tasks.list_board(
            context.principal,
            context.project_id,
            include_completed=include_completed,
            include_discarded=False,
        )
        return {"ok": True, "tasks": board_view(board)}

    async def add_task(
        self,
        title: str,
        description: str,
        estimated_minutes: int,
        needs_research: bool,
        tool_context: ToolContext,
        after_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Add one top-level task to the board.

        Only add a task once you know enough about the goal to size it. A task must fit
        the learner's default task length; if the work is bigger than that, add a task that
        fits and split it, rather than adding an oversized one.

        Args:
            title: A short, concrete description of what the learner will do.
            description: What "done" looks like, in a sentence or two.
            estimated_minutes: How long this should take. Must fit the learner's budget.
            needs_research: True if the learner will need reading or watching material
                prepared for this task.
            after_task_id: Place the new task directly after this one. Omit to append.
        """
        return await self._guarded(
            tool_context,
            self._add_task,
            title,
            description,
            estimated_minutes,
            needs_research,
            after_task_id,
            claim_slot=tool_context,
        )

    async def _add_task(
        self,
        context: AgentContext,
        title: str,
        description: str,
        estimated_minutes: int,
        needs_research: bool,
        after_task_id: str | None,
    ) -> dict[str, Any]:
        if estimated_minutes > context.max_task_minutes:
            raise ValidationProblem(
                f"{estimated_minutes} minutes is more than this project allows for one "
                f"task ({context.max_task_minutes} at most, against a default of "
                f"{context.default_task_minutes}). Add a task that fits and split it."
            )
        task = await self._tasks.create_task(
            context.principal,
            context.project_id,
            title=title,
            description=description,
            estimated_minutes=estimated_minutes,
            after_task_id=after_task_id or None,
            needs_research=needs_research,
            origin=Origin.AGENT,
        )
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def split_task(
        self,
        task_id: str,
        subtasks: list[dict[str, Any]],
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Break one task into between 2 and 8 subtasks that each fit the time budget.

        This is how an oversized piece of work becomes workable: the parent stays on the
        board and shows the count and the summed duration, and the learner works the
        subtasks. A subtask cannot itself be split — if one is still too big, give the
        parent more, smaller subtasks instead.

        Args:
            task_id: The task to split. It must not already have subtasks.
            subtasks: The subtasks to create, in order. Each is an object with `title`, an
                optional `description`, `estimatedMinutes`, and optional `needsResearch`.
        """
        return await self._guarded(tool_context, self._split_task, task_id, subtasks)

    async def _split_task(
        self, context: AgentContext, task_id: str, subtasks: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if not MIN_SPLIT_SUBTASKS <= len(subtasks) <= MAX_SPLIT_SUBTASKS:
            raise ValidationProblem(
                f"A split produces between {MIN_SPLIT_SUBTASKS} and "
                f"{MAX_SPLIT_SUBTASKS} subtasks; you asked for {len(subtasks)}."
            )
        drafts = [_subtask_draft(draft, context) for draft in subtasks]
        parent = await self._tasks.split_task(
            context.principal, task_id, drafts, origin=Origin.AGENT
        )
        await self._announce(context, [parent.id, *(child.id for child in parent.subtasks)])
        return {
            "ok": True,
            "task": task_view(parent),
            "subtasks": [task_view(child) for child in parent.subtasks],
        }

    async def update_task(
        self,
        task_id: str,
        tool_context: ToolContext,
        title: str | None = None,
        description: str | None = None,
        estimated_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Change a task's title, description, or estimate. Omitted fields are left alone.

        Args:
            task_id: The task to change.
            title: A new title, or omit to keep the current one.
            description: A new description, or omit to keep the current one.
            estimated_minutes: A new estimate, or omit to keep the current one.
        """
        return await self._guarded(
            tool_context,
            self._update_task,
            task_id,
            title,
            description,
            estimated_minutes,
        )

    async def _update_task(
        self,
        context: AgentContext,
        task_id: str,
        title: str | None,
        description: str | None,
        estimated_minutes: int | None,
    ) -> dict[str, Any]:
        if estimated_minutes is not None and estimated_minutes > context.max_task_minutes:
            raise ValidationProblem(
                f"{estimated_minutes} minutes is more than this project allows for one "
                f"task ({context.max_task_minutes} at most). Split it instead."
            )
        task = await self._tasks.update_task(
            context.principal,
            task_id,
            title=title,
            description=description,
            estimated_minutes=estimated_minutes,
        )
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def set_task_state(
        self,
        task_id: str,
        state: str,
        tool_context: ToolContext,
        postponed_until: str | None = None,
    ) -> dict[str, Any]:
        """Move a task to a new state.

        Valid states are `not_started`, `current`, `postponed`, and `postponed_until`.
        Only transitions the board allows will succeed. To discard a task, use
        `discard_task` — and **you cannot mark a task complete**: finishing a piece of work
        is the learner's own judgement of it. Say you think they are done and let them
        click.

        Args:
            task_id: The task to move.
            state: The state to move it to.
            postponed_until: Required for `postponed_until`, and only then: an ISO-8601
                timestamp in the future.
        """
        return await self._guarded(
            tool_context, self._set_task_state, task_id, state, postponed_until
        )

    async def _set_task_state(
        self,
        context: AgentContext,
        task_id: str,
        state: str,
        postponed_until: str | None,
    ) -> dict[str, Any]:
        target = _task_state(state)
        if target is TaskState.COMPLETED:
            # docs/10-risks.md Q1: "completion is always the user's click; the agent may
            # *suggest* completion." A guard rather than an instruction, on the same
            # reasoning as `discard_task`'s confirmation — a rule the model can decline to
            # follow is not a rule. Answered as a result so the coach can say so out loud.
            raise ValidationProblem(
                "Only the learner marks a task complete — finishing a piece of work is "
                "their judgement of it, not yours. Say you think they are done and let "
                "them click."
            )
        if target is TaskState.DISCARDED:
            raise ValidationProblem(
                "Use discard_task to propose discarding a task; it asks the learner first."
            )
        task = await self._tasks.set_state(
            context.principal,
            task_id,
            target,
            postponed_until=_timestamp(postponed_until),
        )
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def set_next_up(self, task_id: str, tool_context: ToolContext) -> dict[str, Any]:
        """Make one task the learner's next-up task, demoting whichever was before it.

        Args:
            task_id: The task to pin as next up. It must be waiting to be started.
        """
        return await self._guarded(tool_context, self._set_next_up, task_id)

    async def _set_next_up(self, context: AgentContext, task_id: str) -> dict[str, Any]:
        task = await self._tasks.set_state(context.principal, task_id, TaskState.CURRENT)
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def reorder_task(
        self,
        task_id: str,
        tool_context: ToolContext,
        after_task_id: str | None = None,
        before_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Move a task to sit directly after, or directly before, one of its siblings.

        Give exactly one of `after_task_id` and `before_task_id`.

        Args:
            task_id: The task to move.
            after_task_id: Put it immediately after this sibling.
            before_task_id: Put it immediately before this sibling.
        """
        return await self._guarded(
            tool_context, self._reorder_task, task_id, after_task_id, before_task_id
        )

    async def _reorder_task(
        self,
        context: AgentContext,
        task_id: str,
        after_task_id: str | None,
        before_task_id: str | None,
    ) -> dict[str, Any]:
        task = await self._tasks.reorder(
            context.principal,
            task_id,
            after_task_id=after_task_id or None,
            before_task_id=before_task_id or None,
        )
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def discard_task(
        self, task_id: str, reason: str, tool_context: ToolContext
    ) -> dict[str, Any]:
        """Discard a task the learner no longer needs. **The learner must confirm this.**

        Discarding is close to permanent — a discarded task leaves the board and only the
        learner can restore it — so say why you think it should go and let them decide.

        Args:
            task_id: The task to discard.
            reason: Why this task is no longer worth doing, in the learner's own terms.
        """
        return await self._guarded(tool_context, self._discard_task, task_id, reason)

    async def _discard_task(
        self, context: AgentContext, task_id: str, reason: str
    ) -> dict[str, Any]:
        task = await self._tasks.set_state(context.principal, task_id, TaskState.DISCARDED)
        logger.info(
            "agent discarded a task",
            extra={"task_id": task_id, "project_id": context.project_id, "reason": reason},
        )
        await self._announce(context, [task.id])
        return {"ok": True, "task": task_view(task)}

    async def update_project_prefs(
        self,
        tool_context: ToolContext,
        default_task_minutes: int | None = None,
        research_depth: str | None = None,
        allow_videos: bool | None = None,
    ) -> dict[str, Any]:
        """Change this project's preferences, when the learner has asked you to.

        These override the learner's global settings for this project only. Do not change
        them on your own initiative — they are the learner's statement of how they want to
        work, not a lever for you to pull.

        Args:
            default_task_minutes: How long a task in this project should be.
            research_depth: `light`, `standard`, or `deep`.
            allow_videos: Whether videos may be recommended as material.
        """
        return await self._guarded(
            tool_context,
            self._update_project_prefs,
            default_task_minutes,
            research_depth,
            allow_videos,
        )

    async def _update_project_prefs(
        self,
        context: AgentContext,
        default_task_minutes: int | None,
        research_depth: str | None,
        allow_videos: bool | None,
    ) -> dict[str, Any]:
        # The keys are the whitelist in `ProjectService.WRITABLE_PREF_KEYS`, spelled out
        # one argument at a time rather than taken as a free-form patch: an open patch
        # argument would let the model write any field it invented onto the project.
        patch = {
            key: value
            for key, value in (
                ("defaultTaskMinutes", default_task_minutes),
                ("researchDepth", research_depth),
                ("allowVideos", allow_videos),
            )
            if value is not None
        }
        if not patch:
            raise ValidationProblem("No preference given to change.")
        project = await self._projects.patch(context.principal, context.project_id, prefs=patch)
        prefs = await self._projects.effective_prefs(context.principal, context.project_id)
        # Not a board change, but the board renders the default task length, and the
        # workspace's budget copy is resolved from these.
        await self._announce(context, [])
        return {
            "ok": True,
            "projectId": project.id,
            "effectivePrefs": {
                "defaultTaskMinutes": prefs.default_task_minutes,
                "researchDepth": prefs.research_depth,
                "allowVideos": prefs.allow_videos,
            },
        }

    # --- shared plumbing ---------------------------------------------------------------

    async def _guarded(
        self,
        tool_context: ToolContext,
        handler: Any,
        *args: Any,
        claim_slot: ToolContext | None = None,
    ) -> dict[str, Any]:
        """Resolve the invocation's context, run `handler`, and turn refusals into results.

        `claim_slot` spends one of the run's `add_task` allowance, and does so *after* the
        context resolves and *before* the write — so a refused call does not consume the
        budget and a successful one cannot be repeated past the cap.
        """
        try:
            context = agent_context(tool_context)
            if claim_slot is not None:
                claim_task_slot(claim_slot)
            result: dict[str, Any] = await handler(context, *args)
            return result
        except CoachError as error:
            # Deliberately not `logger.exception`: a guard firing is the system working.
            logger.info("agent tool refused", extra={"code": error.code, "detail": str(error)})
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}

    def as_tools(self) -> list[FunctionTool]:
        """Every domain tool, in the order the model sees them.

        Reads first, then writes: nothing depends on the ordering, but a catalogue that
        starts with "look at the board" reads as one, and the instruction tells the model
        to look before it changes anything.
        """
        return [
            FunctionTool(self.list_tasks),
            FunctionTool(self.add_task),
            FunctionTool(self.split_task),
            FunctionTool(self.update_task),
            FunctionTool(self.set_task_state),
            FunctionTool(self.set_next_up),
            FunctionTool(self.reorder_task),
            # The one gated tool. See the module docstring: ADK turns this into an
            # `adk_request_confirmation` call and runs the body only after the learner
            # answers, so the gate does not depend on the model respecting it.
            FunctionTool(self.discard_task, require_confirmation=True),
            FunctionTool(self.update_project_prefs),
        ]


def _subtask_draft(draft: dict[str, Any], context: AgentContext) -> dict[str, Any]:
    """One subtask, validated against the budget before the transaction opens.

    docs/03-agent-design.md guards `split_task` with "each ≤ default minutes" — a stricter
    bound than `add_task`'s, and the point of splitting: subtasks that individually exceed
    the budget have not solved the problem the split existed for.
    """
    title = str(draft.get("title") or "").strip()
    if not title:
        raise ValidationProblem("Every subtask needs a title.")
    raw_minutes = draft.get("estimatedMinutes", draft.get("estimated_minutes"))
    try:
        minutes = int(raw_minutes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValidationProblem(
            f"Subtask {title!r} needs estimatedMinutes as a whole number of minutes."
        ) from None
    if minutes > context.default_task_minutes:
        raise ValidationProblem(
            f"Subtask {title!r} is {minutes} minutes, over the "
            f"{context.default_task_minutes}-minute budget a subtask has to fit. "
            "Use more, smaller subtasks."
        )
    return {
        "title": title,
        "description": str(draft.get("description") or ""),
        "estimatedMinutes": minutes,
        "needsResearch": bool(draft.get("needsResearch", draft.get("needs_research", True))),
    }


def _task_state(state: str) -> TaskState:
    try:
        return TaskState(state)
    except ValueError:
        allowed = ", ".join(s.value for s in TaskState)
        raise ValidationProblem(
            f"{state!r} is not a task state. Use one of: {allowed}."
        ) from None


def _timestamp(value: str | None) -> datetime | None:
    """An ISO-8601 string from the model, as an aware UTC datetime.

    A naive timestamp is read as UTC rather than refused. `TaskService.set_state` compares
    it against `now()`, which is aware, and comparing the two raises `TypeError` — a bug
    surfacing as a 500 instead of the guard it should have been.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationProblem(
            f"{value!r} is not an ISO-8601 timestamp, e.g. 2026-09-01T09:00:00Z."
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__ = ["DomainTools", "board_view", "task_view"]
