"""Per-invocation prompt assembly.

docs/03-agent-design.md#project_coach-and-task_teacher:

> Dynamic instruction is assembled per-invocation by a `before_agent_callback` that
> injects: effective prefs, current task + its subtasks, last N task outcomes, and the
> `learnerProfile` summary. Injected as state, not as a giant literal prompt, so ADK's
> `{state_key}` templating keeps the prompt cache-friendly.

Two consequences of "as state" that are easy to get wrong:

- **The keys are `temp:`-prefixed.** A session's `state` is stored as a JSON *string*
  (docs/02-data-model.md), so anything written to it is re-serialized onto the session
  document on every appended event. Board state changes every turn and would be written
  every turn, for no reader — ADK trims `temp:` deltas before persistence, which is
  exactly the lifetime this scaffolding wants.
- **The instruction references them with `{temp:coach_board}`, and a missing key is a
  `KeyError` at request-assembly time**, not a blank section. `inject_session_state`
  raises unless the placeholder ends in `?`. Every placeholder here is written
  unconditionally below, and the intake-only ones are still written — as an empty string —
  rather than left out.

The board is rendered as compact text rather than JSON. It is read by a model, not parsed
by one, and the same information as JSON is roughly twice the tokens for no gain in
fidelity; the tools are where structure matters.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from google.adk.agents.context import Context
from google.genai import types

from coach.agents.context import (
    CONFIRM_ITEMS_KEY,
    DEFAULT_MINUTES_KEY,
    PROJECT_ID_KEY,
    RESEARCH_BUDGET_KEY,
    TASK_ID_KEY,
)
from coach.core.errors import CoachError
from coach.core.principal import Principal
from coach.services.models import (
    EffectivePrefs,
    LearnerProfile,
    Task,
    TaskState,
    TaskWithSubtasks,
)
from coach.services.projects import ProjectService
from coach.services.sessions import SessionService
from coach.services.tasks import TaskService
from coach.services.users import UserService

logger = logging.getLogger(__name__)

#: State keys the instruction template reads. Kept beside the template that uses them, so
#: adding a placeholder without writing the key is a visible omission rather than a
#: `KeyError` on the first turn after a deploy.
PREFS_KEY = "temp:coach_prefs"
BOARD_KEY = "temp:coach_board"
FOCUS_KEY = "temp:coach_focus"
OUTCOMES_KEY = "temp:coach_outcomes"
LEARNER_KEY = "temp:coach_learner"
#: Read by `research_agent`'s instruction. Prose rather than a number, because the model has
#: to reason about what is *left* after the reading it has already chosen, and a bare
#: integer in a template invites it to be treated as the answer rather than the ceiling.
BUDGET_TEXT_KEY = "temp:coach_budget_text"

#: How many finished tasks to show as "how this learner has been doing". Enough to read a
#: trend, short enough that it cannot crowd out the live board.
RECENT_OUTCOMES = 5

BeforeAgentCallback = Callable[..., Awaitable[types.Content | None]]


def format_minutes(minutes: int) -> str:
    """`45 min`, `1 h 30 m` — the same shape `apps/web/src/lib/format.ts` renders."""
    if minutes < 60:
        return f"{minutes} min"
    hours, remainder = divmod(minutes, 60)
    return f"{hours} h" if remainder == 0 else f"{hours} h {remainder} m"


def render_prefs(prefs: EffectivePrefs) -> str:
    # The minutes are written as a bare number as well as formatted, and that is load-
    # bearing rather than redundant: `integrations/stub_model.py` reads the budget back
    # out of the rendered instruction, which is what makes golden flow #7 prove the
    # preference reached the model instead of asserting a number the test itself supplied.
    lines = [
        f"- Default task length: {prefs.default_task_minutes} minutes "
        f"({format_minutes(prefs.default_task_minutes)}) — the budget a task must fit",
        f"- Guidance style: {prefs.guidance_style}",
        f"- Guidance level (hands-on guidance amount): {prefs.guidance_level}",
        f"- Verbosity: {prefs.verbosity}",
        f"- Research depth: {prefs.research_depth}; "
        f"videos {'allowed' if prefs.allow_videos else 'not wanted'}",
    ]
    if prefs.preferred_sources:
        sources_str = ", ".join(prefs.preferred_sources)
        lines.append(f"- Topics/sources to prioritize and reinforce: {sources_str}")
    if prefs.avoid_sources:
        sources_str = ", ".join(prefs.avoid_sources)
        lines.append(f"- Topics/sources to skip or avoid: {sources_str}")
    return "\n".join(lines)


def render_task(task: Task, *, indent: str = "") -> str:
    parts = [
        f"{indent}- [{task.state.value}] {task.title} "
        f"({format_minutes(task.estimated_minutes)}, id={task.id})"
    ]
    if task.rollup is not None and task.rollup.subtask_count:
        parts.append(
            f" — {task.rollup.subtask_count} subtasks, "
            f"{format_minutes(task.rollup.total_estimated_minutes)} total"
        )
    elif task.items:
        done = sum(1 for item in task.items if item.completed)
        parts.append(f" — {done} of {len(task.items)} steps done")
    if task.origin.value == "agent":
        parts.append(" — added by you")
    return "".join(parts)


def render_items(task: Task, *, indent: str = "") -> str:
    """A task's checklist — the plan the coach is working through.

    Every item carries its `itemId`, because `complete_task_item` takes one and the model
    has nowhere else to read it from. `details` is included in full: for a guided item they
    are the coach's teaching notes, which is what the coach is for. The *UI* is what must
    never show a guided item's details to the learner
    (docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid).

    `guided` is rendered as a sentence rather than a flag. The distinction decides how the
    coach behaves for the next several minutes — teach this, or hand it over and wait — and
    a bare `guided=false` invites a model to narrate a video it has never seen.
    """
    if not task.items:
        return (
            f"{indent}This task has no checklist yet. If it needs prepared material, "
            "research will write one; otherwise work from the description and add steps "
            "as they come up."
        )
    lines = [f"{indent}The steps, in order:"]
    for item in task.items:
        mark = "x" if item.completed else " "
        budget = f", {format_minutes(item.minutes)}" if item.minutes else ""
        lines.append(f"{indent}  [{mark}] {item.short_description} (id={item.item_id}{budget})")
        if item.guided:
            lines.append(f"{indent}      You walk the learner through this one. Your notes:")
        else:
            lines.append(
                f"{indent}      The learner does this on their own, away from this "
                "conversation — hand it over and wait for them to report back. Do not "
                "describe material you have not read. What they should do:"
            )
        if item.details:
            lines.append(f"{indent}      {item.details}")
        if item.url:
            lines.append(f"{indent}      Link: {item.url}")
    return "\n".join(lines)


def render_board(board: list[TaskWithSubtasks]) -> str:
    if not board:
        return "The board is empty — this project has no tasks yet."
    lines: list[str] = []
    for parent in board:
        lines.append(render_task(parent))
        lines.extend(render_task(child, indent="  ") for child in parent.subtasks)
    return "\n".join(lines)


def render_focus(task: TaskWithSubtasks | None) -> str:
    if task is None:
        return (
            "This conversation is not attached to one task — it is about the project as "
            "a whole."
        )
    lines = [
        f"The learner is working on: {task.title} (id={task.id}, "
        f"{format_minutes(task.estimated_minutes)}, state {task.state.value})"
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.subtasks:
        # **With their checklists.** A parent holds no items of its own, so a focus section
        # that listed subtasks by title alone left the coach unable to see the plan it had
        # just made — no step descriptions, and no item ids, which every item tool needs as
        # an argument. Breaking a task down effectively blinded the coach to it.
        lines.append("Its subtasks, each with its own checklist:")
        for child in task.subtasks:
            lines.append(render_task(child, indent="  "))
            lines.append(render_items(child, indent="    "))
        lines.append(
            "To change a subtask's checklist, pass its id as `subtask_id`. The steps live "
            "on the subtasks now, not on the parent — and `move_task_items` is how work "
            "gets redistributed between them."
        )
    else:
        # A parent's plan is its subtasks and a leaf's is its checklist — never both
        # (docs/02-data-model.md#task-items), so this is an `else` rather than a second
        # section.
        lines.append(render_items(task))
    return "\n".join(lines)


def render_outcomes(board: list[TaskWithSubtasks]) -> str:
    """The last few finished tasks, newest last.

    Ordered by `order` rather than by `completedAt` because the board query does not sort
    on the timestamp and a second query for five lines of prompt is not worth an index.
    Reading them in board order is if anything closer to how the learner experienced them.
    """
    finished = [
        task
        for parent in board
        for task in (parent, *parent.subtasks)
        if task.state is TaskState.COMPLETED
    ]
    if not finished:
        return "No tasks finished yet."
    recent = finished[-RECENT_OUTCOMES:]
    return "\n".join(
        f"- {task.title} ({format_minutes(task.estimated_minutes)})" for task in recent
    )


def render_learner(profile: LearnerProfile) -> str:
    """The `learnerProfile` summary.

    Renders the coach's current beliefs about the learner: thinking style, strengths,
    gaps, skills, pacing, and recent feedback observations.
    """
    lines: list[str] = []
    if profile.thinking_style:
        lines.append(f"- Thinking style: {profile.thinking_style}")
    if profile.strengths:
        lines.append(f"- Strengths: {', '.join(profile.strengths)}")
    if profile.gaps:
        lines.append(f"- Gaps: {', '.join(profile.gaps)}")
    if profile.skills:
        # Each skill carries the subject it was observed in — a belief formed in one
        # subject (e.g. "familiar with simple types" from a Python project) says nothing
        # about the learner's standing in another, so the area is always shown alongside
        # the skill rather than left for a later reader to assume it generalizes.
        known = ", ".join(f"{s.name} ({s.area}, {s.level})" for s in profile.skills)
        lines.append(f"- Skills: {known}")
    if profile.pacing:
        lines.append(f"- Pacing: {profile.pacing}")
    if profile.feedback_notes:
        recent = profile.feedback_notes[-5:]
        lines.append(f"- Recent observations: {'; '.join(recent)}")
    if not lines:
        return "Nothing recorded yet — you are meeting this learner for the first time."
    return "\n".join(lines)


def render_budget(task: TaskWithSubtasks | None, prefs: EffectivePrefs) -> str:
    """The minute budget `research_agent` has to fit its required list inside.

    The task's own estimate, not the project default — a 20-minute task does not get 45
    minutes of required reading because that is what the preference says. The default is
    the fallback for a research run with no task, which `ResearchService` does not start
    but which the prompt has to render something for rather than raise a `KeyError` inside
    a detached generation task.
    """
    if task is None:
        return (
            f"Budget: {prefs.default_task_minutes} minutes for everything in the required "
            "list, added together."
        )
    return (
        f"Budget: {task.estimated_minutes} minutes "
        f"({format_minutes(task.estimated_minutes)}) for everything in the required list, "
        "added together. This is the whole of the time the learner has set aside for this "
        "task, so it has to cover the exercises as well as the reading."
    )


class PromptBuilder:
    """Assembles the invocation's state. Constructed once, called per invocation."""

    def __init__(
        self,
        sessions: SessionService,
        projects: ProjectService,
        tasks: TaskService,
        users: UserService,
    ) -> None:
        self._sessions = sessions
        self._projects = projects
        self._tasks = tasks
        self._users = users

    async def __call__(self, callback_context: Context) -> types.Content | None:
        """`before_agent_callback`.

        Returning `None` lets the agent run; returning `Content` would *replace* the
        model call with that content and end the invocation, which is why every failure
        below is logged and swallowed rather than surfaced here — a board that cannot be
        read should degrade to a coach that cannot see it, not to a turn that answers
        with an error message in the model's voice.
        """
        state = callback_context.state
        principal = Principal(uid=callback_context.user_id, source="agent")
        session_id = callback_context.session.id

        defaults = {
            PREFS_KEY: "",
            BOARD_KEY: "",
            FOCUS_KEY: "",
            OUTCOMES_KEY: "",
            LEARNER_KEY: render_learner(LearnerProfile()),
            BUDGET_TEXT_KEY: render_budget(
                None,
                EffectivePrefs.model_construct(
                    default_task_minutes=45,
                    guidance_style="socratic",
                    verbosity="balanced",
                    timezone="UTC",
                    research_depth="standard",
                    allow_videos=True,
                    preferred_sources=[],
                    avoid_sources=[],
                ),
            ),
            PROJECT_ID_KEY: "",
            TASK_ID_KEY: "",
            DEFAULT_MINUTES_KEY: 45,
            RESEARCH_BUDGET_KEY: 45,
            # The safe default on the failure path too: a board this callback could not
            # read is not a reason to stop asking before completing someone's work.
            CONFIRM_ITEMS_KEY: True,
        }
        state.update(defaults)

        try:
            linkage = await self._sessions.require_owned(principal, session_id)
            if linkage.project_id is None:
                return None
            # `effective_prefs`/`list_board` each call `ProjectService.require_owned`
            # internally, so ownership is verified without a separate call here — this
            # callback has had no use for the `Project` document itself since `PROJECT_KEY`
            # was removed.
            prefs = await self._projects.effective_prefs(principal, linkage.project_id)
            board = await self._tasks.list_board(
                principal,
                linkage.project_id,
                include_completed=True,
                include_discarded=False,
            )
            user = await self._users.get_or_create(principal)
        except CoachError:
            # An unlinked or unreadable session is not a reason to fail the turn; the
            # coach simply has no board in front of it and the tools will say so.
            logger.warning(
                "prompt assembly could not read the board", extra={"session": session_id}
            )
            return None

        focus = next(
            (task for task in board if task.id == linkage.task_id),
            None,
        )
        state.update(
            {
                PREFS_KEY: render_prefs(prefs),
                BOARD_KEY: render_board(board),
                FOCUS_KEY: render_focus(focus),
                OUTCOMES_KEY: render_outcomes(board),
                LEARNER_KEY: render_learner(user.learner_profile),
                BUDGET_TEXT_KEY: render_budget(focus, prefs),
                PROJECT_ID_KEY: linkage.project_id,
                TASK_ID_KEY: linkage.task_id or "",
                DEFAULT_MINUTES_KEY: prefs.default_task_minutes,
                CONFIRM_ITEMS_KEY: prefs.confirm_item_completion,
                # The number behind the prose above. `post_research_report` validates
                # against this rather than re-parsing the instruction.
                RESEARCH_BUDGET_KEY: (
                    focus.estimated_minutes if focus is not None else prefs.default_task_minutes
                ),
            }
        )
        return None


__all__ = [
    "BOARD_KEY",
    "BUDGET_TEXT_KEY",
    "FOCUS_KEY",
    "LEARNER_KEY",
    "OUTCOMES_KEY",
    "PREFS_KEY",
    "RECENT_OUTCOMES",
    "PromptBuilder",
    "format_minutes",
    "render_board",
    "render_budget",
    "render_focus",
    "render_items",
    "render_learner",
    "render_outcomes",
    "render_prefs",
    "render_task",
]
