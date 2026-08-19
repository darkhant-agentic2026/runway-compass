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
from coach.services.tasks import TaskService
from coach.ws.hub import BoardUpdateHub

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
    ) -> None:
        self._tasks = tasks
        self._projects = projects
        self._hub = hub

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
            **(
                {"sourceCompleted": True}
                if source.state is TaskState.COMPLETED
                else {}
            ),
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
            tool_context, self._complete_task_item, item_id, note, subtask_id
        )

    async def _complete_task_item(
        self, context: AgentContext, item_id: str, note: str, subtask_id: str
    ) -> dict[str, Any]:
        task_id = await self._item_task(context, subtask_id)
        task = await self._tasks.patch_item(context.principal, task_id, item_id, completed=True)
        logger.info(
            "agent completed a checklist item",
            extra={"task_id": task_id, "item_id": item_id, "note": note},
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
            FunctionTool(self.update_task),
            FunctionTool(self.set_task_state),
            FunctionTool(self.set_next_up),
            FunctionTool(self.reorder_task),
            # The one gated tool. See the module docstring: ADK turns this into an
            # `adk_request_confirmation` call and runs the body only after the learner
            # answers, so the gate does not depend on the model respecting it.
            FunctionTool(self.discard_task, require_confirmation=True),
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
            FunctionTool(self.delete_task_item, require_confirmation=True),
            # Not `require_confirmation=True`: this tool asks for a *choice*, not for
            # approval, so it posts its own confirmation carrying the question. See
            # `_ask_learner`.
            FunctionTool(self.ask_learner),
            # The second gated tool, and the more consequential of the two: completing the
            # last item completes the task (docs/02-data-model.md#task-items), so this is
            # what keeps "completion is the learner's click" true now that a task can
            # finish itself.
            FunctionTool(self.complete_task_item, require_confirmation=True),
            FunctionTool(self.update_project_prefs),
        ]


def _checklist_budget(task: Task, context: AgentContext) -> dict[str, Any]:
    """How long the checklist has grown, against what the learner asked a task to be.

    **Guidance, not a guard.** docs/02-data-model.md#task-items has no rule about a
    checklist's total, and there should not be one: a 50-minute plan on a 45-minute task is
    a rounding difference, and refusing it would be the tool overruling a judgement the
    coach is better placed to make with the learner in front of it. What the tool owes the
    model is the *fact* — a running total it would otherwise have to keep in its head across
    several calls, which is exactly the kind of arithmetic a model quietly gets wrong.

    Reported only when there is something to report. A checklist inside its budget needs no
    comment, and a field that is always present is one the model learns to skip.
    """
    planned = sum(item.minutes or 0 for item in task.items)
    budget = context.default_task_minutes
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
