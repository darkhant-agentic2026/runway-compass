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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from google.adk.memory.memory_entry import MemoryEntry
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.load_memory_tool import load_memory
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from coach.agents.context import (
    CONFIRM_ITEMS_KEY,
    AgentContext,
    agent_context,
    claim_profile_update_slot,
    claim_task_slot,
)
from coach.core.app import APP_NAME
from coach.core.errors import CoachError, ValidationProblem
from coach.services.models import (
    Origin,
    RoadmapBrief,
    SkillBelief,
    Task,
    TaskState,
    TaskWithSubtasks,
)
from coach.services.projects import ProjectService
from coach.services.sessions import SessionService
from coach.services.study_plans import DEFAULT_MATERIALIZE_DECISIONS, StudyPlanService
from coach.services.tasks import TaskService
from coach.ws.hub import BoardUpdateHub

if TYPE_CHECKING:
    from coach.adk_firestore import CoachMemoryService
    from coach.services.research import ResearchService
    from coach.services.users import UserService

logger = logging.getLogger(__name__)

#: How many options a question may offer. Below two it is not a choice; above six it is a
#: list to read rather than a control to use, and prose is the better instrument.
MIN_CHOICES = 2
MAX_CHOICES = 6

#: Marks a confirmation payload as a question rather than a yes/no gate. The client
#: switches on it (`apps/web/src/lib/transcript.ts`), which cannot import this module —
#: the same restated-constant arrangement as `adk_request_confirmation` itself, and it is
#: on the bump checklist for the same reason.
QUESTION_PAYLOAD_KIND = "coach_question"


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


def items_view(task: Task) -> list[dict[str, Any]]:
    """The task's checklist, as the model sees it after changing it.

    `details` is included because this is the coach's own working copy — for a guided item
    it is the teaching material it needs in order to teach. The *UI* is what must not render
    a guided item's details (docs/06-frontend.md); the model is exactly who they are for.
    """
    return [
        {
            "itemId": item.item_id,
            "shortDescription": item.short_description,
            "details": item.details,
            "guided": item.guided,
            "completed": item.completed,
            **({"minutes": item.minutes} if item.minutes is not None else {}),
            **({"url": item.url} if item.url else {}),
        }
        for item in task.items
    ]


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
        users: UserService | None = None,
        memory: CoachMemoryService | None = None,
        study_plans: StudyPlanService | None = None,
        sessions: SessionService | None = None,
        research_provider: Callable[[], ResearchService] | None = None,
    ) -> None:
        self._tasks = tasks
        self._projects = projects
        self._hub = hub
        self._users = users
        self._memory = memory
        self._plans = study_plans
        self._sessions = sessions
        # A provider, not the service itself — `coach/core/lazy.py`'s pattern, applied for
        # the same reason it exists there: `api/deps.py` builds `DomainTools` before
        # `ResearchService` (whose own construction needs the queue, which needs the
        # executor, which needs the turn service, which needs `DomainTools` — building
        # `ResearchService` first would be circular). A zero-argument callable resolved
        # only when `propose_roadmap_brief` actually schedules a run breaks the cycle
        # without either service reaching for the other at import time.
        self._research_provider = research_provider

    # --- scoping -----------------------------------------------------------------------

    async def _item_task(self, context: AgentContext, task_id: str | None) -> str:
        """Which task an item tool is acting on: this conversation's, or one of its subtasks.

        Item tools used to take **no** task id at all, on the reasoning that a task-scoped
        session is the only place they are useful and an argument naming a task would be a
        way to point them somewhere else. That was right about the risk and wrong about the
        scope: breaking a task down makes the session's task a *parent*, and a parent holds
        no checklist — so every item tool silently stopped working the moment the coach did
        the thing it had just been asked to do. The checklist was on the subtask, and
        nothing could reach it.

        So the argument is back, bounded rather than free: the session's own task, or one of
        its children. That keeps the property the original reasoning was protecting — a tool
        cannot be pointed at another project, or at an unrelated task in this one — while
        making a broken-down task's actual plan editable.

        One read rather than the whole board: `parentTaskId` on the candidate is enough to
        decide, and `resolve` has already checked ownership.
        """
        session_task = _require_task(context)
        if not task_id or task_id == session_task:
            return session_task

        candidate = await self._tasks.resolve(context.principal, task_id)
        if candidate.parent_task_id != session_task:
            raise ValidationProblem(
                "That task is not the one this conversation is about, nor one of its "
                "subtasks. You can only change the checklist of the task in front of the "
                "learner and the subtasks under it."
            )
        return task_id

    # --- fan-out -----------------------------------------------------------------------

    async def _announce(self, context: AgentContext, task_ids: list[str]) -> None:
        """Tell the learner's other tabs which tasks moved.

        **The session's own task is always included.** The client turns each named id into
        an invalidation of `['task', id]`, and the task workspace is keyed on the task the
        conversation is about — so a tool that changed a *subtask* would name ids the open
        screen does not read, and the screen would not refresh. `move_task_items` names two
        subtasks and nothing else; without this the learner would watch the coach say it had
        moved their steps and see nothing move.

        This is the third outing for the same shape: a push that reaches the screens the
        writer had in mind rather than the ones that exist
        (docs/09-roadmap.md#five-more-rows-for-the-table-above). Adding the id here rather
        than at each call site is what stops the next tool rediscovering it.
        """
        focus = [context.task_id] if context.task_id else []
        # `dict.fromkeys` rather than a set: the order is what the client iterates, and a
        # set would make it vary between runs for no reason.
        named = list(dict.fromkeys([*task_ids, *focus]))
        await self._hub.publish(
            context.principal.uid,
            project_id=context.project_id,
            task_ids=named,
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

    async def add_subtask(
        self,
        task_id: str,
        title: str,
        description: str,
        estimated_minutes: int,
        needs_research: bool,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Add one subtask under an existing task, making it a composite task.

        Use this when a single piece of work turns out to have a distinct part worth
        tracking on its own — not to restructure a task the learner is happy with. If you
        already know the whole breakdown, `split_task` does it in one call.

        **The first subtask inherits the parent's checklist.** A task's plan is either its
        items or its subtasks, never both, so the steps that were on the parent move onto
        this subtask along with anything the learner had already ticked off. Say so when
        you do it, and put the subtask where those steps belong.

        Args:
            task_id: The task to add a subtask to. It must be a top-level task.
            title: A short, concrete description of what the learner will do.
            description: What "done" looks like, in a sentence or two.
            estimated_minutes: How long this subtask should take.
            needs_research: True if this subtask will need its own prepared material.
        """
        return await self._guarded(
            tool_context,
            self._add_subtask,
            task_id,
            title,
            description,
            estimated_minutes,
            needs_research,
            claim_slot=tool_context,
        )

    async def _add_subtask(
        self,
        context: AgentContext,
        task_id: str,
        title: str,
        description: str,
        estimated_minutes: int,
        needs_research: bool,
    ) -> dict[str, Any]:
        if estimated_minutes > context.default_task_minutes:
            # The stricter of the two bounds, matching `split_task`'s: a subtask exists to
            # make a piece of work fit, and one that does not fit has not done that.
            raise ValidationProblem(
                f"{estimated_minutes} minutes is over the "
                f"{context.default_task_minutes}-minute budget a subtask has to fit. "
                "Use more, smaller subtasks."
            )
        child = await self._tasks.create_task(
            context.principal,
            context.project_id,
            title=title,
            description=description,
            estimated_minutes=estimated_minutes,
            parent_task_id=task_id,
            needs_research=needs_research,
            origin=Origin.AGENT,
        )
        await self._announce(context, [task_id, child.id])
        return {
            "ok": True,
            "task": task_view(child),
            "parentTaskId": task_id,
            # Spelled out because it is a consequence the model has to tell the learner
            # about, and one it cannot see from the child's view alone.
            "inheritedItems": len(child.items),
            "items": items_view(child),
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

        Valid states are `not_started`, `in_progress`, `postponed`, and `postponed_until`.
        Only transitions the board allows will succeed. To discard a task, use
        `discard_task` — and **you cannot mark a task complete**: finishing a piece of work
        is the learner's own judgement of it. If every item on its checklist is done, the
        task completes itself; otherwise say you think they are done and let them click.

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
            #
            # M4 gave completion a second route — a leaf task whose checklist is finished
            # completes itself — and this guard is what keeps that route honest. Every item
            # is ticked either by the learner or through `complete_task_item`, which asks
            # first, so the derivation is always downstream of a click. Opening this door
            # would put a way round that in the model's hands.
            raise ValidationProblem(
                "Only the learner marks a task complete — finishing a piece of work is "
                "their judgement of it, not yours. Complete its checklist items instead "
                "(complete_task_item asks them first), or say you think they are done."
            )
        if target is TaskState.DISCARDED:
            raise ValidationProblem(
                "Use discard_task to propose discarding a task; it asks the learner first."
            )
        if target is TaskState.DRAFT:
            raise ValidationProblem(
                "'draft' is a state a task leaves, not one it is put into: it means the "
                "task has no plan yet. Give it items or subtasks instead."
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
        """Move one task to the front of the board, so it is what the learner picks up next.

        This changes the order of the board; it does not start the task or finish anything
        else. Use it when the conversation has established that something should come
        first.

        Args:
            task_id: The task to move to the front. It must be a top-level task.
        """
        return await self._guarded(tool_context, self._set_next_up, task_id)

    async def _set_next_up(self, context: AgentContext, task_id: str) -> dict[str, Any]:
        # Until M4 this promoted the task to `current`, which was singular and therefore
        # *was* the next-up pointer. `in_progress` is not singular and `nextUpTaskId` is
        # derived from `order` (docs/02-data-model.md#task-state-machine), so pinning
        # something is a reorder — and, unlike the old behaviour, it no longer silently
        # un-starts whatever the learner had open.
        board = await self._tasks.list_board(
            context.principal, context.project_id, include_completed=False
        )
        first = next((t for t in board if t.id != task_id), None)
        if first is None:
            raise ValidationProblem(
                "That is already the only task on the board; there is nothing to move it "
                "in front of."
            )
        task = await self._tasks.reorder(context.principal, task_id, before_task_id=first.id)
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

    async def add_task_items(
        self,
        items: list[dict[str, Any]],
        tool_context: ToolContext,
        subtask_id: str = "",
    ) -> dict[str, Any]:
        """Add steps to this task's checklist, in the order they should be worked.

        The checklist is what the learner has to get through for this task to be done, so
        add something here only when the conversation has turned up real work the prepared
        materials did not anticipate — not to restate what is already on the list.

        Args:
            items: The steps to append, in order. Each is an object with
                `shortDescription` (one line, what the learner will do), `details` (for a
                step you will walk them through: your notes for teaching it; for one they
                go and do alone: the instruction itself, with any link), `guided` (true if
                you will work through it with them in this conversation, false if they go
                away and do it), and optional `minutes` and `url`.
            subtask_id: Leave empty for the task in front of the learner. When it has
                been broken down, name the subtask whose checklist you mean — the steps live
                on the subtasks then, not on the parent.
        """
        return await self._guarded(tool_context, self._add_task_items, items, subtask_id)

    async def _add_task_items(
        self, context: AgentContext, items: list[dict[str, Any]], subtask_id: str
    ) -> dict[str, Any]:
        task_id = await self._item_task(context, subtask_id)
        task = await self._tasks.add_items(context.principal, task_id, items)
        await self._announce(context, [task.id])
        return {
            "ok": True,
            "task": task_view(task),
            "items": items_view(task),
            **_checklist_budget(task, context),
        }

    async def update_task_item(
        self,
        item_id: str,
        tool_context: ToolContext,
        short_description: str | None = None,
        details: str | None = None,
        guided: bool | None = None,
        subtask_id: str = "",
    ) -> dict[str, Any]:
        """Reword a step, change its notes, or change who does it. Omitted fields are left.

        Args:
            item_id: The step to change, from the checklist in your context.
            short_description: A new one-line description of what the learner will do.
            details: New notes. For a step you guide, your material for teaching it; for
                one they do alone, the instruction itself.
            guided: True if you will now work through this with them, false if they should
                go and do it on their own.
            subtask_id: Leave empty for the task in front of the learner. When it has
                been broken down, name the subtask whose checklist you mean — the steps live
                on the subtasks then, not on the parent.
        """
        return await self._guarded(
            tool_context,
            self._update_task_item,
            item_id,
            short_description,
            details,
            guided,
            subtask_id,
        )

    async def _update_task_item(
        self,
        context: AgentContext,
        item_id: str,
        short_description: str | None,
        details: str | None,
        guided: bool | None,
        subtask_id: str,
    ) -> dict[str, Any]:
        task = await self._tasks.patch_item(
            context.principal,
            await self._item_task(context, subtask_id),
            item_id,
            short_description=short_description,
            details=details,
            guided=guided,
        )
        await self._announce(context, [task.id])
        return {"ok": True, "items": items_view(task)}

    async def reorder_task_item(
        self,
        item_id: str,
        tool_context: ToolContext,
        after_item_id: str | None = None,
        before_item_id: str | None = None,
        subtask_id: str = "",
    ) -> dict[str, Any]:
        """Move a step earlier or later in the checklist.

        The order is the order the work happens in — reading before the exercise that uses
        it, setup before the thing being set up — so move a step when the conversation
        shows the sequence was wrong. Give exactly one of the two anchors.

        Args:
            item_id: The step to move.
            after_item_id: Put it immediately after this step.
            before_item_id: Put it immediately before this step.
            subtask_id: Leave empty for the task in front of the learner. When it has
                been broken down, name the subtask whose checklist you mean — the steps live
                on the subtasks then, not on the parent.
        """
        return await self._guarded(
            tool_context,
            self._reorder_task_item,
            item_id,
            after_item_id,
            before_item_id,
            subtask_id,
        )

    async def _reorder_task_item(
        self,
        context: AgentContext,
        item_id: str,
        after_item_id: str | None,
        before_item_id: str | None,
        subtask_id: str,
    ) -> dict[str, Any]:
        task = await self._tasks.reorder_item(
            context.principal,
            await self._item_task(context, subtask_id),
            item_id,
            after_item_id=after_item_id or None,
            before_item_id=before_item_id or None,
        )
        await self._announce(context, [task.id])
        return {"ok": True, "items": items_view(task)}

    async def move_task_items(
        self,
        item_ids: list[str],
        to_subtask_id: str,
        tool_context: ToolContext,
        from_subtask_id: str = "",
    ) -> dict[str, Any]:
        """Move steps from one task's checklist onto another's.

        **This is how you redistribute work after breaking a task down.** Adding the first
        subtask hands it the *whole* checklist, because a task's plan is its items or its
        subtasks and never both — so the steps that belong to the second subtask start out
        on the first, and this is what moves them. Do it in one call per destination rather
        than a step at a time.

        Nothing is lost: a step keeps its identity and stays ticked if the learner had
        already done it. Use this rather than deleting and re-adding, which throws away
        what they had finished and asks them to approve every removal.

        Args:
            item_ids: The steps to move, in the order they should sit in their new home.
            to_subtask_id: The task to move them onto — a subtask of the one in front of the
                learner, or that task itself. It must not itself have subtasks.
            from_subtask_id: Where they are now. Leave empty for the task in front of the
                learner; after a breakdown that is the parent and holds nothing, so name the
                subtask that inherited them.
        """
        return await self._guarded(
            tool_context, self._move_task_items, item_ids, to_subtask_id, from_subtask_id
        )

    async def _move_task_items(
        self,
        context: AgentContext,
        item_ids: list[str],
        to_subtask_id: str,
        from_subtask_id: str,
    ) -> dict[str, Any]:
        source_id = await self._item_task(context, from_subtask_id)
        target_id = await self._item_task(context, to_subtask_id)
        source, target = await self._tasks.move_items(
            context.principal,
            from_task_id=source_id,
            to_task_id=target_id,
            item_ids=item_ids,
        )
        await self._announce(context, [source.id, target.id])
        return {
            "ok": True,
            "moved": len(item_ids),
            "from": {"taskId": source.id, "items": items_view(source)},
            "to": {"taskId": target.id, "items": items_view(target)},
            # Moving the last *outstanding* step off a task finishes it — the work has not
            # gone anywhere, it is on the other task now, so that is a true statement rather
            # than a completion nobody asked for. Reported so the coach says it out loud.
            **({"sourceCompleted": True} if source.state is TaskState.COMPLETED else {}),
        }

    async def delete_task_item(
        self,
        item_id: str,
        reason: str,
        tool_context: ToolContext,
        subtask_id: str = "",
    ) -> dict[str, Any]:
        """Remove a step from the checklist. **The learner must confirm this.**

        Only for a step that should not have been there — one that turned out to be
        irrelevant, or that duplicates another. A step the learner has decided not to
        bother with is theirs to leave unticked, not yours to delete.

        Args:
            item_id: The step to remove.
            reason: Why this step should not be on the list, in one sentence.
            subtask_id: Leave empty for the task in front of the learner. When it has
                been broken down, name the subtask whose checklist you mean — the steps live
                on the subtasks then, not on the parent.
        """
        return await self._guarded(
            tool_context, self._delete_task_item, item_id, reason, subtask_id
        )

    async def _delete_task_item(
        self, context: AgentContext, item_id: str, reason: str, subtask_id: str
    ) -> dict[str, Any]:
        task = await self._tasks.delete_item(
            context.principal, await self._item_task(context, subtask_id), item_id
        )
        logger.info(
            "agent deleted a checklist item",
            extra={"task_id": task.id, "item_id": item_id, "reason": reason},
        )
        await self._announce(context, [task.id])
        return {
            "ok": True,
            "items": items_view(task),
            "taskCompleted": task.state is TaskState.COMPLETED,
            **_checklist_budget(task, context),
        }

    async def complete_task_item(
        self,
        item_id: str,
        note: str,
        tool_context: ToolContext,
        subtask_id: str = "",
    ) -> dict[str, Any]:
        """Mark one checklist item done. **The learner must confirm this.**

        Completing the last outstanding item completes the whole task, so this is their
        call rather than yours: say what you saw them do and let them agree. Do not use it
        to tidy up items they have not actually worked through.

        Args:
            item_id: The item to mark done, from the checklist in your context.
            note: What the learner did that finished this, in a sentence.
            subtask_id: Leave empty for the task in front of the learner. When it has
                been broken down, name the subtask whose checklist you mean — the steps live
                on the subtasks then, not on the parent.
        """
        return await self._guarded(
            tool_context, self._complete_task_item, item_id, note, subtask_id, tool_context
        )

    async def _complete_task_item(
        self,
        context: AgentContext,
        item_id: str,
        note: str,
        subtask_id: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        task_id = await self._item_task(context, subtask_id)
        task = await self._tasks.patch_item(context.principal, task_id, item_id, completed=True)

        # "…and stop asking for this project", the dialog's third button. It rides in the
        # confirmation's payload rather than being a second request from the client, so the
        # learner's one click is one round trip and the preference cannot land without the
        # completion it was attached to.
        silenced = False
        if _answer_asks_to_stop_confirming(tool_context):
            await self._projects.patch(
                context.principal,
                context.project_id,
                prefs={"confirmItemCompletion": False},
            )
            silenced = True

        logger.info(
            "agent completed a checklist item",
            extra={
                "task_id": task_id,
                "item_id": item_id,
                "note": note,
                "confirmation_disabled": silenced,
            },
        )
        await self._announce(context, [task.id])
        return {
            "ok": True,
            "task": task_view(task),
            "items": items_view(task),
            # Spelled out rather than left for the model to infer from `state`: the
            # completion is a consequence of this call, and a coach that does not notice it
            # happened will congratulate the learner on one item and miss the task.
            "taskCompleted": task.state is TaskState.COMPLETED,
            # So the coach can say it out loud. A preference that changed silently is one
            # the learner discovers by noticing an absence.
            **({"confirmationDisabledForProject": True} if silenced else {}),
        }

    async def ask_learner(
        self,
        question: str,
        options: list[str],
        allow_multiple: bool,
        allow_none: bool,
        tool_context: ToolContext,
        note_prompt: str = "",
    ) -> dict[str, Any]:
        """Ask the learner to pick from a short list, and wait for their answer.

        Use this instead of asking in prose whenever the answer is a choice you can
        enumerate — which of these should come first, which of these do you already know,
        do you want the video or the article. It puts real controls in front of them
        rather than asking them to type a number back, and their answer is recorded in the
        conversation where you can both see it later.

        Ask for **several answers** (`allow_multiple`) whenever more than one could be
        true at once — what they already know, which parts they want covered, which of
        these they have tried. Single choice is for questions whose answers exclude each
        other, like what to do first.

        Do **not** use it for open questions, for anything with more than about six
        options, or to ask permission for something you have a tool for. Asking whether to
        discard a task, delete a step, or mark one done is what those tools' own
        confirmations are for.

        This tool waits. You will get the answer as its result; do not guess at it, and do
        not ask the same question twice.

        Args:
            question: What you are asking, in one sentence, addressed to the learner.
            options: The choices, in the order they should be shown. Two to six of them,
                each a short phrase rather than a sentence.
            allow_multiple: **Use this whenever more than one answer could be true at
                once.** "Which of these have you used before", "which parts feel shaky",
                "which of these would you like covered" are all several-answer questions,
                and forcing them into one choice makes the learner pick the least wrong
                option and lose the rest. Reserve false for questions where the answers
                genuinely exclude each other — what to do *first*, which single article to
                read.
            allow_none: True if "none of these" is a real answer to your question.
            note_prompt: If they should be able to add a comment, the label for that box —
                for example "Anything else I should know?". Leave empty for no comment box.
        """
        return await self._guarded(
            tool_context,
            self._ask_learner,
            question,
            options,
            allow_multiple,
            allow_none,
            note_prompt,
            tool_context,
        )

    async def _ask_learner(
        self,
        _context: AgentContext,
        question: str,
        options: list[str],
        allow_multiple: bool,
        allow_none: bool,
        note_prompt: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Post the question, or read the answer.

        **Two invocations, one tool.** ADK's confirmation handshake is what makes a tool
        able to wait for a human (`google/adk/flows/llm_flows/request_confirmation.py`):
        the first call asks, the invocation ends, and the *same* call is re-executed once
        the learner answers, with `tool_context.tool_confirmation` populated. So the body
        below is "have I been answered yet?" rather than two tools with a state machine
        between them.

        `request_confirmation` is called **here rather than through
        `FunctionTool(require_confirmation=True)`**, and that is the whole reason this
        works: the static flag posts ADK's own generic hint and no payload, where this
        needs to carry the question and its options to the client. A dynamically requested
        confirmation is a first-class case in the processor — it checks
        `requires_confirmation or requested_in_history`.
        """
        answer = getattr(tool_context, "tool_confirmation", None)
        if answer is not None:
            return _answer_view(answer, options)

        cleaned = [option.strip() for option in options if option.strip()]
        if not MIN_CHOICES <= len(cleaned) <= MAX_CHOICES:
            raise ValidationProblem(
                f"A question needs between {MIN_CHOICES} and {MAX_CHOICES} options; you "
                f"gave {len(cleaned)}. Ask in prose if the answer is not a short list."
            )
        if len(set(cleaned)) != len(cleaned):
            raise ValidationProblem("Two of those options are the same. Make them distinct.")

        tool_context.request_confirmation(
            hint=question.strip(),
            # The client renders the dialog from this. It rides on the
            # `adk_request_confirmation` function call's args, which the transcript
            # already reads for the yes/no prompt.
            payload={
                "kind": QUESTION_PAYLOAD_KIND,
                "question": question.strip(),
                "options": cleaned,
                "allowMultiple": bool(allow_multiple),
                "allowNone": bool(allow_none),
                "notePrompt": note_prompt.strip(),
            },
        )
        # ADK would otherwise summarise this holding answer into prose and say it out
        # loud; the dialog is already on screen and the coach has nothing to add yet.
        tool_context.actions.skip_summarization = True
        return {"ok": True, "status": "waiting_for_the_learner", "question": question.strip()}

    async def update_project_prefs(
        self,
        default_task_minutes: int | None = None,
        guidance_level: str | None = None,
        guidance_style: str | None = None,
        preferred_sources: list[str] | None = None,
        avoid_sources: list[str] | None = None,
        research_depth: str | None = None,
        allow_videos: bool | None = None,
        confirm_item_completion: bool | None = None,
        *,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Change this project's preferences, when the learner has asked you to.

        These override the learner's global settings for this project only. Do not change
        them on your own initiative — they are the learner's statement of how they want to
        work, not a lever for you to pull.

        Args:
            default_task_minutes: How long a task in this project should be.
            guidance_level: Amount of guidance desired for task items: `mostly_guided`,
                `balanced`, or `mostly_unguided`.
            guidance_style: Socratic inquiry vs direct explanations: `socratic`, `direct`,
                or `mixed`.
            preferred_sources: Topics, subjects, or material sources to reinforce
                and prioritize.
            avoid_sources: Topics, subjects, or material sources to skip or avoid.
            research_depth: `light`, `standard`, or `deep`.
            allow_videos: Whether videos may be recommended as material.
            confirm_item_completion: Whether the coach must ask before ticking items complete.
        """
        return await self._guarded(
            tool_context,
            self._update_project_prefs,
            default_task_minutes,
            guidance_level,
            guidance_style,
            preferred_sources,
            avoid_sources,
            research_depth,
            allow_videos,
            confirm_item_completion,
        )

    async def _update_project_prefs(
        self,
        context: AgentContext,
        default_task_minutes: int | None,
        guidance_level: str | None,
        guidance_style: str | None,
        preferred_sources: list[str] | None,
        avoid_sources: list[str] | None,
        research_depth: str | None,
        allow_videos: bool | None,
        confirm_item_completion: bool | None,
    ) -> dict[str, Any]:
        # The keys are the whitelist in `ProjectService.WRITABLE_PREF_KEYS`, spelled out
        # one argument at a time rather than taken as a free-form patch: an open patch
        # argument would let the model write any field it invented onto the project.
        patch = {
            key: value
            for key, value in (
                ("defaultTaskMinutes", default_task_minutes),
                ("guidanceLevel", guidance_level),
                ("guidanceStyle", guidance_style),
                ("preferredSources", preferred_sources),
                ("avoidSources", avoid_sources),
                ("researchDepth", research_depth),
                ("allowVideos", allow_videos),
                ("confirmItemCompletion", confirm_item_completion),
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
            "effectivePrefs": prefs.to_document(),
        }

    async def update_project_plan(
        self,
        tasks: list[dict[str, Any]],
        summary: str = "",
        description: str | None = None,
        *,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Propose or update the overall project plan with a list of tasks.

        This requires user confirmation. The learner can either 'Accept plan' (which applies
        these tasks to their board) or choose to 'Keep refining' with additional feedback.

        Args:
            tasks: List of proposed tasks, each with title, description, optional
                estimated_minutes, and optional subtasks.
            summary: Brief overview or rationale of the proposed plan.
            description: Refined one- or two-sentence project description, if updated
                through discussion.
        """
        return await self._guarded(
            tool_context,
            self._update_project_plan,
            tasks,
            summary,
            description,
        )

    async def _update_project_plan(
        self,
        context: AgentContext,
        tasks: list[dict[str, Any]],
        summary: str = "",
        description: str | None = None,
    ) -> dict[str, Any]:
        if not tasks:
            raise ValidationProblem("A plan must contain at least one task.")

        if description:
            await self._projects.patch(
                context.principal, context.project_id, description=description
            )

        created_tasks: list[Task] = []
        for task_dict in tasks:
            title = str(task_dict.get("title", "")).strip()
            if not title:
                continue
            desc = str(task_dict.get("description", ""))
            raw_minutes = task_dict.get("estimated_minutes") or task_dict.get(
                "estimatedMinutes"
            )
            minutes = int(raw_minutes) if raw_minutes is not None else None
            needs_res = bool(
                task_dict.get("needs_research", task_dict.get("needsResearch", True))
            )

            created = await self._tasks.create_task(
                context.principal,
                context.project_id,
                title=title,
                description=desc,
                estimated_minutes=minutes,
                needs_research=needs_res,
                origin=Origin.AGENT,
            )
            created_tasks.append(created)

            raw_subtasks = task_dict.get("subtasks")
            if isinstance(raw_subtasks, list):
                for sub in raw_subtasks:
                    if isinstance(sub, dict):
                        sub_title = str(sub.get("title", "")).strip()
                        if not sub_title:
                            continue
                        sub_desc = str(sub.get("description", ""))
                        sub_min = sub.get("estimated_minutes") or sub.get("estimatedMinutes")
                        sub_minutes = int(sub_min) if sub_min is not None else None
                        sub_res = bool(
                            sub.get("needs_research", sub.get("needsResearch", True))
                        )
                        await self._tasks.create_task(
                            context.principal,
                            context.project_id,
                            title=sub_title,
                            description=sub_desc,
                            estimated_minutes=sub_minutes,
                            parent_task_id=created.id,
                            needs_research=sub_res,
                            origin=Origin.AGENT,
                        )

        await self._announce(context, [t.id for t in created_tasks])

        return {
            "ok": True,
            "accepted": True,
            "summary": summary,
            "tasksCount": len(created_tasks),
            "tasks": [task_view(t) for t in created_tasks],
        }

    # --- roadmap brief ---------------------------------------------------------------------
    #
    # The structured intake for a roadmap run. `write_roadmap_brief`/`read_roadmap_brief`
    # let `project_coach` draft and re-read the brief across turns before proposing it;
    # `propose_roadmap_brief` is the confirmation-gated handoff — approval renders the
    # stored draft and schedules it as a roadmap run, the same way `update_project_plan`'s
    # approval creates the tasks it proposed.

    async def _session_attachment_names(self, context: AgentContext) -> list[str]:
        """Display names of every file actually attached in this conversation, oldest
        first, as `SessionService.list_attachments` stored them — not lowercased, so it
        can be shown to the model verbatim when a name it gave does not match one."""
        if self._sessions is None or context.session_id is None:
            return []
        attachments = await self._sessions.list_attachments(
            context.principal, context.session_id
        )
        return [a["displayName"] for a in attachments if a.get("displayName")]

    async def _validate_attachment_names(
        self, context: AgentContext, names: list[str] | None
    ) -> list[str]:
        """Only a filename that matches an upload actually present in this conversation
        may land on a roadmap brief.

        Without this, a name the model invented — a title it inferred from the file's
        contents rather than the filename it was shown — would pass `write_roadmap_brief`
        silently, then vanish at `ResearchService.start_roadmap`'s own matching against
        the same list (`docs/02-data-model.md#projectsprojectid`): no attachment on the
        run, no chip in the confirmation dialog, and nothing to explain why to the
        learner. Refusing here instead is loud in the one place that can still do
        something about it — the model gets the *real* list back and can retry against
        it, rather than the learner discovering the gap after the run has already started.
        """
        cleaned = [name.strip() for name in (names or []) if name.strip()]
        if not cleaned:
            return []
        known = await self._session_attachment_names(context)
        known_lower = {name.lower() for name in known}
        invalid = [name for name in cleaned if name.lower() not in known_lower]
        if invalid:
            available = ", ".join(known) if known else "none"
            raise ValidationProblem(
                f"{', '.join(invalid)} — no file with that name is attached to this "
                f"conversation. Files actually attached here: {available}. Use the exact "
                "filename shown for the file, never a title or description inferred from "
                "its contents, or leave attachments out if none belong on this brief."
            )
        return cleaned

    async def write_roadmap_brief(
        self,
        subject: str,
        time_budget: str,
        tool_context: ToolContext,
        specific_topics: list[str] | None = None,
        additional_notes: str = "",
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write or update this project's roadmap brief — the structured intake for a
        roadmap run, drafted across turns before you propose it.

        Call this as the conversation establishes each piece, not only once at the end; a
        later call replaces the whole draft, so include everything you have gathered so
        far, not only what changed. `read_roadmap_brief` reads it back.

        Args:
            subject: The main subject the learner plans to learn. Required.
            time_budget: The learner's total study time budget, in their own words —
                "4 lessons", "two months", "four weeks, 5 sessions a week". Required:
                combine a vague answer with what you already know of their pacing
                preferences rather than leaving this empty.
            specific_topics: Sub-topics or aspects of `subject` they want covered.
            additional_notes: Anything else that shapes the research — depth (quick,
                standard, deep), topics to skip or emphasize, preferred material types.
            attachments: Filenames of any files the learner attached that are relevant to
                this roadmap (a syllabus, a job posting, prior notes) — exactly the
                filename shown for the file, never a title or description you inferred
                from its contents. This conversation may have other attachments unrelated
                to the roadmap; list only the ones that matter to it, since only these are
                carried onto the roadmap run's opening message. A name that does not match
                an actual attachment in this conversation is refused, not silently
                accepted — you will get the real list back to correct it against. If this
                is the first call for this brief and the conversation has attachments you
                did not list, the result names them back to you — reconsider whether any
                belong on the brief before calling this again.
        """
        return await self._guarded(
            tool_context,
            self._write_roadmap_brief,
            subject,
            time_budget,
            specific_topics,
            additional_notes,
            attachments,
        )

    async def _write_roadmap_brief(
        self,
        context: AgentContext,
        subject: str,
        time_budget: str,
        specific_topics: list[str] | None,
        additional_notes: str,
        attachments: list[str] | None,
    ) -> dict[str, Any]:
        subject = subject.strip()
        time_budget = time_budget.strip()
        if not subject:
            raise ValidationProblem(
                "A roadmap brief needs a subject — what the learner plans to learn."
            )
        if not time_budget:
            raise ValidationProblem(
                "A roadmap brief needs a time budget — how much study time the learner "
                "has, even a rough one."
            )
        validated_attachments = await self._validate_attachment_names(context, attachments)
        # Read before writing, so "is this the first call" reflects what was there before
        # this write rather than what `set_roadmap_brief` is about to make true.
        existing = await self._projects.require_owned(context.principal, context.project_id)
        is_first_call = existing.roadmap_brief is None

        project = await self._projects.set_roadmap_brief(
            context.principal,
            context.project_id,
            subject=subject,
            time_budget=time_budget,
            specific_topics=specific_topics,
            additional_notes=additional_notes,
            attachments=validated_attachments,
        )
        assert project.roadmap_brief is not None
        result: dict[str, Any] = {
            "ok": True,
            "roadmapBrief": project.roadmap_brief.to_document(),
        }

        # A nudge, not a guard: nothing stops the model from ignoring this, but a model
        # that never considered attachments at all — the defect a real coach-dev run
        # actually hit — gets one unprompted chance to reconsider, on the one call where
        # bringing it up cannot yet read as nagging about something already decided.
        # Scoped to the *first* call only: a later call with attachments still empty is
        # more likely a deliberate "these files don't belong on this brief" than an
        # oversight, and repeating the nudge on every call would be exactly that nagging.
        if is_first_call and not validated_attachments:
            available = await self._session_attachment_names(context)
            if available:
                result["availableAttachments"] = available
        return result

    async def read_roadmap_brief(self, tool_context: ToolContext) -> dict[str, Any]:
        """Read back this project's current roadmap brief draft.

        `None` if nothing has been written yet, or if the last one was already used to
        start a run — `propose_roadmap_brief` clears the draft once it schedules one.
        """
        return await self._guarded(tool_context, self._read_roadmap_brief)

    async def _read_roadmap_brief(self, context: AgentContext) -> dict[str, Any]:
        project = await self._projects.require_owned(context.principal, context.project_id)
        return {
            "ok": True,
            "roadmapBrief": (
                project.roadmap_brief.to_document() if project.roadmap_brief else None
            ),
        }

    async def propose_roadmap_brief(
        self,
        subject: str,
        time_budget: str,
        tool_context: ToolContext,
        specific_topics: list[str] | None = None,
        additional_notes: str = "",
        attachments: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ask the learner to approve the roadmap brief. **Requires user confirmation.**

        Reproduce the brief exactly as `write_roadmap_brief` last stored it — the same
        values, not a fresh guess — so what the learner reviews is the actual stored
        draft. **The confirmation dialog renders the project's stored brief document
        directly, not these arguments** — so a mismatch here would not merely mislead the
        learner, it would show them something other than what they are approving.
        Approval renders it and schedules a roadmap run for the project as a whole, with
        the referenced attachments carried onto its opening message; declining leaves the
        draft as it was, for you to keep refining with the learner.

        **The dialog's own attachment checklist, if the learner changes it, wins over
        this call's `attachments` argument** — the learner may tick or untick files right
        there without another round of conversation, and the server applies their
        selection deterministically rather than asking you to notice and re-propose.

        Args:
            subject: The main subject, exactly as last written.
            time_budget: The time budget, exactly as last written.
            specific_topics: The specific topics, exactly as last written.
            additional_notes: The additional notes, exactly as last written.
            attachments: The referenced attachment filenames, exactly as last written. A
                name that does not match an actual attachment in this conversation is
                refused, the same check `write_roadmap_brief` makes. Only the starting
                point for what the learner sees — see above.
        """
        return await self._guarded(
            tool_context,
            self._propose_roadmap_brief,
            subject,
            time_budget,
            specific_topics,
            additional_notes,
            attachments,
            tool_context,
        )

    async def _propose_roadmap_brief(
        self,
        context: AgentContext,
        subject: str,
        time_budget: str,
        specific_topics: list[str] | None,
        additional_notes: str,
        attachments: list[str] | None,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Runs only once the learner has confirmed — the static `require_confirmation`
        gate on `FunctionTool`, same mechanism `update_project_plan` and `discard_task`
        use (module docstring)."""
        if self._research_provider is None:
            raise ValidationProblem("Roadmap scheduling is not configured.")
        subject = subject.strip()
        time_budget = time_budget.strip()
        if not subject or not time_budget:
            raise ValidationProblem(
                "A roadmap brief needs both a subject and a time budget before it can be "
                "proposed."
            )
        # The dialog's own checklist is authoritative once it has one to offer — the
        # learner's final word, applied deterministically, not the model's possibly-stale
        # guess from before the dialog was even shown. Falls back to the model's argument
        # only when the answer carries no checklist selection at all (`None`, not `[]` —
        # see `_confirmed_attachments`'s own docstring for why that distinction matters).
        confirmed = _confirmed_attachments(tool_context)
        validated_attachments = await self._validate_attachment_names(
            context, confirmed if confirmed is not None else attachments
        )
        # Re-stored rather than trusted from the arguments alone, so the draft on record
        # always matches what was actually confirmed — cheap, and it is what
        # `read_roadmap_brief` shows afterwards if the run fails to start.
        brief = RoadmapBrief(
            subject=subject,
            time_budget=time_budget,
            specific_topics=[t.strip() for t in (specific_topics or []) if t.strip()],
            additional_notes=additional_notes.strip(),
            attachments=validated_attachments,
        )
        await self._projects.set_roadmap_brief(
            context.principal,
            context.project_id,
            subject=brief.subject,
            time_budget=brief.time_budget,
            specific_topics=brief.specific_topics,
            attachments=brief.attachments,
            additional_notes=brief.additional_notes,
        )

        if context.session_id is None:
            raise ValidationProblem(
                "This conversation has no session to start the roadmap run from."
            )
        research = self._research_provider()
        run = await research.start_roadmap(
            context.principal,
            context.session_id,
            reason=brief.render(),
            attachment_names=brief.attachments,
        )
        await self._projects.clear_roadmap_brief(context.principal, context.project_id)
        return {
            "ok": True,
            "scheduled": True,
            "runId": run.id,
            "sessionId": run.session_id,
        }

    # --- study plans ---------------------------------------------------------------------

    async def view_study_plan(self, tool_context: ToolContext) -> dict[str, Any]:
        """Read this project's most recent study plan — a roadmap run's own result, or a
        later revision of one, whichever was written most recently.

        `None` if the project has no plan yet. Look here before `revise_study_plan` or
        `materialize_study_plan`: both act on a specific `planId`, which this returns.
        """
        return await self._guarded(tool_context, self._view_study_plan)

    async def _view_study_plan(self, context: AgentContext) -> dict[str, Any]:
        if self._plans is None:
            raise ValidationProblem("Study plan storage is not configured.")
        plan = await self._plans.get_latest(context.principal, context.project_id)
        return {"ok": True, "studyPlan": plan.to_document() if plan is not None else None}

    async def revise_study_plan(
        self,
        plan_id: str,
        plan: list[dict[str, Any]],
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Re-decide a study plan's inclusion and ordering, as your own copy.

        Writes a **new** plan rather than changing `plan_id` — the original stays exactly
        as `plan_tailor` (or an earlier revision) wrote it, so it stays legible against
        whatever this replaces it with. `view_study_plan` afterwards returns the copy,
        since it is now the most recent.

        Args:
            plan_id: The plan to revise, from `view_study_plan`.
            plan: One entry per proposed task on that plan — every one of them, including
                any you leave unchanged. Each is an object with `task_slug`, `after` (the
                slug of the proposed task this one should sit directly after once
                materialized, or omit), `prerequisite_tasks`, `relevance` (0-4),
                `decision` (`include`, `additional`, `exclude`, or `reject`), and `why`
                (addressed to the learner, required even for an excluded or rejected
                task).
        """
        return await self._guarded(tool_context, self._revise_study_plan, plan_id, plan)

    async def _revise_study_plan(
        self, context: AgentContext, plan_id: str, plan: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if self._plans is None:
            raise ValidationProblem("Study plan storage is not configured.")
        revised = await self._plans.revise(
            context.principal, project_id=context.project_id, plan_id=plan_id, plan=plan
        )
        included = sum(
            1 for entry in revised.plan if entry.decision in ("include", "additional")
        )
        return {
            "ok": True,
            "planId": revised.id,
            "revisedFromPlanId": plan_id,
            "taskCount": len(revised.proposed_tasks),
            "includedCount": included,
        }

    async def materialize_study_plan(
        self,
        plan_id: str,
        tool_context: ToolContext,
        decisions: list[str] | None = None,
        project_description: str = "",
    ) -> dict[str, Any]:
        """Create board tasks — fully prepared, with items — from a written study plan.
        **Requires user confirmation** — this is the final approval that puts a plan's
        tasks on the board, the study-plan analogue of `update_project_plan`'s.

        Turns the plan's `include` and `additional` (deep-dive) tasks into real tasks on
        the board, in dependency order, each already carrying the material `task_proposer`
        gathered for it. `exclude`/`reject` tasks are never created — their `why` stays on
        the plan document. Calling this twice for the same plan is safe: the second call
        returns the tasks the first one already created rather than making a second set.

        The confirmation dialog offers a checkbox, "Also update project description" —
        checked by default when the project has none yet. Provide `project_description`
        every time regardless of whether you expect it to be checked; the learner's
        answer, not your guess, decides whether it is used.

        Args:
            plan_id: The study plan to materialize.
            decisions: Which decisions to create tasks for. Omit for the default
                (`include` and `additional`).
            project_description: A single factual sentence describing what the project is
                now for, based on this plan — write it as a description of the project,
                not as a summary of the plan (e.g. "Learning React fundamentals and
                building a portfolio app", not "A roadmap covering React basics, hooks,
                and a final project"). Only applied if the learner leaves the checkbox
                checked.
        """
        return await self._guarded(
            tool_context,
            self._materialize_study_plan,
            plan_id,
            decisions,
            project_description,
            tool_context,
        )

    async def _materialize_study_plan(
        self,
        context: AgentContext,
        plan_id: str,
        decisions: list[str] | None,
        project_description: str,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        if self._plans is None:
            raise ValidationProblem("Study plan storage is not configured.")
        chosen = frozenset(decisions) if decisions else DEFAULT_MATERIALIZE_DECISIONS
        created = await self._plans.materialize(
            context.principal,
            project_id=context.project_id,
            plan_id=plan_id,
            decisions=chosen,  # type: ignore[arg-type]
        )

        # "…and update the project description", the approval dialog's checkbox. Applied
        # from the confirmation's own payload, not from whether `project_description` is
        # merely non-empty — the model always supplies a candidate (docstring), and only
        # the learner's answer decides whether it lands.
        description_updated = False
        if project_description.strip() and _answer_asks_to_update_description(tool_context):
            await self._projects.patch(
                context.principal, context.project_id, description=project_description.strip()
            )
            description_updated = True

        await self._announce(context, [task.id for task in created])
        return {
            "ok": True,
            "createdCount": len(created),
            "tasks": [task_view(task) for task in created],
            **({"projectDescriptionUpdated": True} if description_updated else {}),
        }

    # --- learner profile & memory tools (M7) -------------------------------------------

    async def update_learner_profile(
        self,
        thinking_style: str | None = None,
        strengths: list[str] | None = None,
        gaps: list[str] | None = None,
        skills: list[dict[str, Any] | SkillBelief] | None = None,
        pacing: str | None = None,
        feedback_note: str | None = None,
        *,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Update beliefs about how this learner thinks and works.

        Call this when you observe something significant about their learning style,
        strengths, knowledge gaps, skills, or pacing. Rate-limited to 1 update per turn.
        Every update is audited and visible to the learner in Settings.

        Args:
            thinking_style: Description of their thinking style (≤ 500 chars).
            strengths: Observed strengths or mastered concepts.
            gaps: Knowledge gaps or areas needing reinforcement.
            skills: List of skill beliefs, each with `name`, `area` (the subject or
                technology this skill was observed in — e.g. "Python", "linear algebra",
                "prose writing"; use "general" only for a skill that is not tied to one
                subject), `level`, and `evidence`. A skill observed in one subject says
                nothing about the learner's standing in another — always set `area` to
                the subject you actually observed it in, never the project's overall
                topic, so a belief formed while studying one language or field is not
                misapplied when the learner later studies a different one.
            pacing: Learner's preferred pacing (e.g. "Fast-paced, prefers dense material").
            feedback_note: Specific feedback note or observation from this session.
        """
        return await self._guarded(
            tool_context,
            self._update_learner_profile,
            thinking_style,
            strengths,
            gaps,
            skills,
            pacing,
            feedback_note,
            claim_profile_slot=tool_context,
        )

    async def _update_learner_profile(
        self,
        context: AgentContext,
        thinking_style: str | None = None,
        strengths: list[str] | None = None,
        gaps: list[str] | None = None,
        skills: list[dict[str, Any] | SkillBelief] | None = None,
        pacing: str | None = None,
        feedback_note: str | None = None,
    ) -> dict[str, Any]:
        if self._users is None:
            raise ValidationProblem("User service is not configured.")

        profile = await self._users.agent_update_learner_profile(
            context.principal,
            thinking_style=thinking_style,
            strengths=strengths,
            gaps=gaps,
            skills=skills,
            pacing=pacing,
            feedback_note=feedback_note,
            session_id=context.session_id,
        )
        return {
            "ok": True,
            "learnerProfile": profile.to_document(),
        }

    async def remember(
        self,
        text: str,
        tags: list[str] | None = None,
        *,
        tool_context: ToolContext,
    ) -> dict[str, Any]:
        """Store a durable memory item to recall in future sessions.

        Args:
            text: The key insight, preference, or concept to remember.
            tags: Optional topic tags (e.g. ["asyncio", "debugging"]).
        """
        return await self._guarded(
            tool_context,
            self._remember,
            text,
            tags,
        )

    async def _remember(
        self,
        context: AgentContext,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if self._memory is None:
            raise ValidationProblem("Memory service is not configured.")

        entry = MemoryEntry(
            content=types.Content(role="user", parts=[types.Part(text=text)]),
            author="agent",
            custom_metadata={
                "tags": tags or [],
                "sourceSessionId": context.session_id,
                "projectId": context.project_id,
            },
        )
        await self._memory.add_memory(
            app_name=APP_NAME,
            user_id=context.principal.uid,
            memories=[entry],
        )
        return {"ok": True, "remembered": text, "tags": tags or []}

    # --- shared plumbing ---------------------------------------------------------------

    async def _guarded(
        self,
        tool_context: ToolContext,
        handler: Any,
        *args: Any,
        claim_slot: ToolContext | None = None,
        claim_profile_slot: ToolContext | None = None,
        **kwargs: Any,
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
            if claim_profile_slot is not None:
                claim_profile_update_slot(claim_profile_slot)
            result: dict[str, Any] = await handler(context, *args, **kwargs)
            return result
        except CoachError as error:
            # Deliberately not `logger.exception`: a guard firing is the system working.
            logger.info("agent tool refused", extra={"code": error.code, "detail": str(error)})
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}

    def as_project_tools(self) -> list[FunctionTool]:
        """The board-level catalogue — `project_coach`'s tools.

        docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher:
        this agent reasons about the board as a whole and has **no item-level tool at
        all** — nothing here can touch a checklist, because a checklist belongs to one
        task and this conversation is never about one task.

        `add_subtask` is here as well as on `as_task_tools`: golden flow #2 breaks an
        oversized task into subtasks in the same intake turn that created it, so the
        agent that just called `add_task` has to be able to follow it with `add_subtask`
        without a second conversation. What it does *not* have is any checklist tool —
        the subtask it creates is a new entry on the board, not a step inside one.

        Reads first, then writes: nothing depends on the ordering, but a catalogue that
        starts with "look at the board" reads as one, and the instruction tells the model
        to look before it changes anything.
        """
        return [
            FunctionTool(self.list_tasks),
            FunctionTool(self.add_task),
            FunctionTool(self.update_task),
            FunctionTool(self.set_task_state),
            FunctionTool(self.set_next_up),
            FunctionTool(self.reorder_task),
            # The one gated tool. See the module docstring: ADK turns this into an
            # `adk_request_confirmation` call and runs the body only after the learner
            # answers, so the gate does not depend on the model respecting it.
            FunctionTool(self.discard_task, require_confirmation=True),
            FunctionTool(self.add_subtask),
            # Not `require_confirmation=True`: this tool asks for a *choice*, not for
            # approval, so it posts its own confirmation carrying the question. See
            # `_ask_learner`.
            FunctionTool(self.ask_learner),
            FunctionTool(self.update_project_prefs),
            FunctionTool(self.update_project_plan, require_confirmation=True),
            FunctionTool(self.write_roadmap_brief),
            FunctionTool(self.read_roadmap_brief),
            FunctionTool(self.propose_roadmap_brief, require_confirmation=True),
            FunctionTool(self.view_study_plan),
            FunctionTool(self.revise_study_plan),
            FunctionTool(self.materialize_study_plan, require_confirmation=True),
            FunctionTool(self.update_learner_profile),
            FunctionTool(self.remember),
            FunctionTool(load_memory),
        ]

    def as_task_tools(self) -> list[FunctionTool]:
        """The checklist catalogue — `task_teacher`'s tools.

        docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher:
        this agent is the conversation about one task, and everything it can create —
        `add_subtask`, a checklist step — is scoped inside that task. **There is no
        `add_task` here.** That is the fix for the reported bug: a learner describing an
        extra topic for the task in front of them used to reach the coach's `add_task`,
        which puts a new entry beside the task on the board rather than inside it. An
        agent that cannot call `add_task` cannot make that mistake, whatever the prompt
        says.

        `discard_task` is here for the same reason it is on `as_project_tools`: a learner
        can say "discard this" from inside the task's own conversation as easily as from
        the board, and the gate is the same one either way.
        """
        return [
            FunctionTool(self.list_tasks),
            FunctionTool(self.add_subtask),
            FunctionTool(self.add_task_items),
            FunctionTool(self.update_task_item),
            FunctionTool(self.reorder_task_item),
            # Not gated, unlike `delete_task_item`, and the difference is that nothing is
            # lost. Deleting the last outstanding step completes a task by making work
            # *vanish*; moving it completes the source because the work is now visibly on
            # another task, which is a true statement about where it is. Gating it would
            # also make redistributing a ten-step checklist ten approvals, which is the
            # cost that sent people back to deleting in the first place.
            FunctionTool(self.move_task_items),
            # Gated for two reasons, and the second is the one that is easy to miss:
            # removing a step is destructive, *and* removing the last outstanding one
            # completes the task (invariant 6). Left ungated it would be a route around
            # `complete_task_item`'s confirmation — the same hole `set_task_state` closes
            # by refusing `completed` and `discarded`.
            #
            # Not subject to the project opt-out below. That preference is about the
            # *friction* of confirming routine completions; deleting a step is neither
            # routine nor recoverable, and a learner who turned off one did not ask for the
            # other.
            FunctionTool(self.delete_task_item, require_confirmation=True),
            # Not `require_confirmation=True`: this tool asks for a *choice*, not for
            # approval, so it posts its own confirmation carrying the question. See
            # `_ask_learner`.
            FunctionTool(self.ask_learner),
            # The second gated tool, and the more consequential of the two: completing the
            # last item completes the task (docs/02-data-model.md#task-items), so this is
            # what keeps "completion is the learner's click" true now that a task can
            # finish itself.
            #
            # **A callable rather than `True`**, so a project can turn the gate off. ADK
            # supports either (`FunctionTool.check_require_confirmation`), and evaluating it
            # per call is what makes the preference take effect on the very next completion
            # rather than at the next process restart. It reads `temp:` state rather than
            # the project document because this runs while the tool call is being assembled,
            # and a Firestore read on that path would be one per gated call.
            FunctionTool(self.complete_task_item, require_confirmation=_confirm_completions),
            FunctionTool(self.discard_task, require_confirmation=True),
            FunctionTool(self.update_project_prefs),
            FunctionTool(self.update_learner_profile),
            FunctionTool(self.remember),
            FunctionTool(load_memory),
        ]

    def as_autonomous_tools(self) -> list[FunctionTool]:
        """The reduced set an unattended run may use.

        docs/03-agent-design.md#safety-rails-on-autonomy and
        docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change:

        > Allowed: `add_task` (≤ 5), `add_subtask`, `reorder_task`, `set_next_up`,
        > `post_research_report`, read-only tools.
        > Forbidden: `discard_task`, `set_task_state` to `completed`,
        > `update_learner_profile`, `update_project_prefs`, anything touching another
        > project.

        Built by **enumerating what is allowed**, not by removing what is not. A subtractive
        list is one that silently re-admits every tool added afterwards: the next
        destructive tool this class grows would be autonomous by default, and nothing would
        report it.

        The confirmation-gated tools are absent for a second, independent reason — there is
        nobody to answer. `discard_task`, `delete_task_item`, `complete_task_item`, and
        `ask_learner` all end the invocation waiting for a human who, by construction, is
        not there; including them would turn a run into a turn that stops halfway and
        leaves a question in a transcript nobody is reading.

        `set_task_state` is here, and its own guard is what keeps it safe: the tool already
        refuses `completed`, `discarded`, and `draft` for every caller
        (docs/09-roadmap.md#status-after-m4), so "to `completed`" needs no second check on
        this path.

        `post_research_report` is not in this list because it belongs to `ResearchTools`,
        which the research step reaches through its own agent.
        """
        return [
            FunctionTool(self.list_tasks),
            FunctionTool(self.add_task),
            FunctionTool(self.add_subtask),
            FunctionTool(self.update_task),
            FunctionTool(self.set_task_state),
            FunctionTool(self.set_next_up),
            FunctionTool(self.reorder_task),
            FunctionTool(self.add_task_items),
        ]


#: The flag the dialog's third button sets in its answer payload.
#:
#: Restated in `apps/web/src/components/session/ConfirmationPrompt.tsx`, which cannot import
#: it — the same arrangement as `adk_request_confirmation` and the question payload's kind,
#: and on the ADK bump checklist for the same reason.
STOP_CONFIRMING_KEY = "stopConfirming"


def _answer_asks_to_stop_confirming(tool_context: ToolContext) -> bool:
    """Whether the learner used the "and stop asking" button rather than plain approval."""
    answer = getattr(tool_context, "tool_confirmation", None)
    payload = getattr(answer, "payload", None)
    return isinstance(payload, dict) and payload.get(STOP_CONFIRMING_KEY) is True


#: The flag the study-plan approval dialog's "Also update project description" checkbox
#: sets in its answer payload — mirrors `STOP_CONFIRMING_KEY`'s restated-constant
#: arrangement with `apps/web/src/components/session/ConfirmationPrompt.tsx`.
UPDATE_PROJECT_DESCRIPTION_KEY = "updateProjectDescription"


def _answer_asks_to_update_description(tool_context: ToolContext) -> bool:
    """Whether the learner left the study-plan approval's description checkbox checked."""
    answer = getattr(tool_context, "tool_confirmation", None)
    payload = getattr(answer, "payload", None)
    return isinstance(payload, dict) and payload.get(UPDATE_PROJECT_DESCRIPTION_KEY) is True


#: The key the roadmap brief approval dialog's own attachment checklist sets in its
#: answer payload — mirrors `STOP_CONFIRMING_KEY`'s restated-constant arrangement with
#: `apps/web/src/components/session/ConfirmationPrompt.tsx`.
CONFIRMED_ATTACHMENTS_KEY = "confirmedAttachments"


def _confirmed_attachments(tool_context: ToolContext) -> list[str] | None:
    """The learner's own final selection from the approval dialog's attachment
    checklist, if the dialog that answered this confirmation offered one.

    `None` — not `[]` — when the key is absent, which is what tells
    `_propose_roadmap_brief` to fall back to the model's own `attachments` argument
    rather than to "nothing": a caller with no checklist to offer (a direct tool call in
    a test, say) must not have its argument silently discarded, but the dialog's own
    checklist — once it renders at all — is authoritative over whatever the model most
    recently guessed, deterministically and without another model turn to reconcile the
    two, which is the reason this checklist exists.
    """
    answer = getattr(tool_context, "tool_confirmation", None)
    payload = getattr(answer, "payload", None)
    if not isinstance(payload, dict) or CONFIRMED_ATTACHMENTS_KEY not in payload:
        return None
    raw = payload.get(CONFIRMED_ATTACHMENTS_KEY)
    if not isinstance(raw, list):
        return None
    return [str(name) for name in raw]


def _confirm_completions(tool_context: ToolContext, **_: Any) -> bool:
    """Whether this project wants to be asked before a step is ticked.

    docs/02-data-model.md: `confirmItemCompletion`, on unless a project turns it off. Off is
    for a project of short, obvious tasks, where a dialog per step is friction rather than a
    safeguard.

    **`**_` is load-bearing.** ADK invokes this callable with the *tool's* arguments, not
    with a context: `check_require_confirmation` builds them through
    `_prepare_invocation_args`, which filters against `FunctionTool.func`'s signature and
    then calls `callable(**args)`. So this receives `item_id`, `note`, `subtask_id` and
    `tool_context` — and a one-parameter version raises `TypeError` *inside the flow*, where
    it surfaces as `DynamicNodeFailError` and a failed turn rather than as anything naming
    the gate. Swallowing the rest by keyword also means the tool can grow an argument
    without this breaking.

    **Defaults to asking**, on a missing key as much as on an unreadable board. Q1 rests on
    this click (docs/10-risks.md#open-questions), so the failure mode of a preference that
    could not be resolved has to be the safe one — `not False` is a cheaper mistake than
    `not True`.
    """
    return tool_context.state.get(CONFIRM_ITEMS_KEY, True) is not False


def _checklist_budget(task: Task, context: AgentContext) -> dict[str, Any]:
    """How long the checklist has grown, against what the learner asked a task to be.

    **Guidance, not a guard.** docs/02-data-model.md#task-items has no rule about a
    checklist's total, and there should not be one: a 50-minute plan on a 45-minute task is
    a rounding difference, and refusing it would be the tool overruling a judgement the
    coach is better placed to make with the learner in front of it. What the tool owes the
    model is the *fact* — a running total it would otherwise have to keep in its head across
    several calls, which is exactly the kind of arithmetic a model quietly gets wrong.

    The budget is the task's own `estimated_minutes` override if set, falling back to the
    project's `default_task_minutes`.

    Reported only when there is something to report. A checklist inside its budget needs no
    comment, and a field that is always present is one the model learns to skip.
    """
    planned = sum(item.minutes or 0 for item in task.items)
    budget = task.estimated_minutes if task.estimated_minutes else context.default_task_minutes
    if planned <= budget:
        return {}
    return {
        "plannedMinutes": planned,
        "taskBudgetMinutes": budget,
        "note": (
            f"This checklist now runs to {planned} minutes against a {budget}-minute task. "
            "Consider whether it is really two pieces of work — `add_subtask` moves the "
            "steps onto the first subtask."
        ),
    }


def _answer_view(answer: Any, options: list[str]) -> dict[str, Any]:
    """The learner's reply, as the model sees it.

    A declined question is a *result*, not a failure: "none of these" is frequently the
    honest answer and the coach needs to be able to act on it rather than retry.

    The selection is filtered against the options that were offered. The payload comes
    back through the client, so treating it as authoritative would let a hand-made request
    put arbitrary text into the model's context — a small surface, and free to close.
    """
    payload = getattr(answer, "payload", None) or {}
    if not getattr(answer, "confirmed", False):
        return {"ok": True, "answered": False, "selected": [], "note": ""}
    raw = payload.get("selected") if isinstance(payload, dict) else None
    offered = set(options)
    selected = [str(choice) for choice in (raw or []) if str(choice) in offered]
    note = str(payload.get("note") or "") if isinstance(payload, dict) else ""
    return {
        "ok": True,
        "answered": True,
        "selected": selected,
        "note": note[:2000],
    }


def _require_task(context: AgentContext) -> str:
    """The task this conversation is about."""
    if context.task_id is None:
        raise ValidationProblem(
            "This conversation is about the project as a whole rather than one task, so "
            "there is no checklist to change. Open the task to work through its items."
        )
    return context.task_id


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
