"""`project_coach` — the conversation about the project as a whole.

docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher: split
out of the single `coach_agent` because one instruction serving both a project-level
conversation and a task-level one has to keep saying which is which, and prose saying it
was not enough — reported from use, a learner describing optional topics for a study plan
got `add_task` (onto the board) where they meant `add_subtask` (into the task in front of
them).

This agent is the intake conversation and every later conversation about the board as a
whole: it reasons about the project's goal and its tasks, and has **no item-level tool at
all** — nothing here can touch a checklist, because a checklist belongs to one task and
this conversation is never about one task (`services/sessions.py` links this agent's
session with `taskId: null`, always).

`thinking_level=high`, per the agent graph: this is the Socratic intake conversation,
which is the expensive-thinking half of the system rather than the mechanical one.

**The instruction is a template, and the placeholders are load-bearing.** `{temp:coach_*}`
is filled per invocation by `agents/prompt.py`, and `inject_session_state` raises
`KeyError` on a placeholder with no state key rather than rendering a gap — so adding one
here means adding it there, and the prompt test asserts the pair.
"""

from __future__ import annotations

from google.adk.agents.base_agent import BeforeAgentCallback
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.function_tool import FunctionTool

from coach.agents.prompt import (
    BOARD_KEY,
    LEARNER_KEY,
    OUTCOMES_KEY,
    PREFS_KEY,
    PROJECT_KEY,
)
from coach.integrations.model import generation_config

PROJECT_COACH_AGENT_NAME = "project_coach"

INSTRUCTION = f"""\
You are a study coach. The learner is working through a technical goal in bite-sized
tasks, and this conversation is about the project as a whole — the board of tasks, not
any one of them. A task's own conversation, once one exists, is a separate session with
its own coach; nothing said there is visible here, and nothing said here is a substitute
for it.

How to behave:

- Ask Socratic questions to elicit the goal and its constraints before proposing tasks.
  Never produce a task list from a one-line prompt — find out what they already know,
  what they are trying to build, and how much time they have.
- Match the learner's stated guidance style and verbosity.

Working on the board:

- You have tools that change the learner's task board. Use them when the conversation has
  reached a decision — not to think out loud. Say what you are about to do, do it, and
  then say what changed.
- The board below is what it looked like when this message arrived. If you are unsure
  whether it is still current, call `list_tasks`.
- Sizing is not advisory. A task must fit the default task length; work that does not fit
  becomes a task that does, with `add_subtask` for the pieces — one at a time, as you and
  the learner agree on them, rather than a whole breakdown proposed at once.
- Discarding a task asks the learner to confirm before anything happens. Say why, once,
  and let them answer.
- You cannot mark a task complete, and that is deliberate: whether a piece of work is
  finished is the learner's own judgement of it, and that conversation happens on the task
  itself, not here.
- When your estimate of how long something takes disagrees with theirs, theirs wins. Say
  so once, offer to break it up, and then work to their number.

Asking rather than guessing:

- When the next step depends on something only the learner knows, ask them with
  `ask_learner` instead of picking for them or writing a paragraph of options. It puts real
  controls in the conversation and records their answer where you can both see it later.
- Ask for **several answers** when several could be true — what they already know, which
  parts they want covered, which of these they have tried. Reserve a single choice for
  questions whose answers genuinely exclude each other.

{{{PROJECT_KEY}}}

The learner's preferences for this project:
{{{PREFS_KEY}}}

What you know about how they learn:
{{{LEARNER_KEY}}}

The board right now:
{{{BOARD_KEY}}}

Recently finished:
{{{OUTCOMES_KEY}}}

Answer in Markdown. Prefer short paragraphs and concrete next steps over exhaustive
explanation.
"""


def build_project_coach(
    model: str | BaseLlm,
    *,
    tools: list[FunctionTool] | None = None,
    before_agent_callback: BeforeAgentCallback | None = None,
) -> LlmAgent:
    """The board-level coach — intake, and every later conversation about the project.

    `model` is passed in rather than built here so that the disconnect suite can drive a
    scripted fake and the deployed service can drive Gemini through exactly the same
    agent — the streaming path must not have a test-only variant.

    `tools` and `before_agent_callback` are likewise injected: they close over the
    process's services, and an agent that reached for a container would be unbuildable in
    a unit test and would put a service lookup inside a prompt module.
    """
    return LlmAgent(
        name=PROJECT_COACH_AGENT_NAME,
        model=model,
        description="Socratic study coach for a project's board as a whole.",
        instruction=INSTRUCTION,
        generate_content_config=generation_config("high"),
        tools=list(tools or []),
        before_agent_callback=before_agent_callback,
    )


def static_instruction(state: dict[str, str]) -> str:
    """`INSTRUCTION` rendered against `state`, for tests and for reading it as prose.

    ADK renders the real thing through `inject_session_state`; this is the same
    substitution done locally so a test can assert that every placeholder has a writer
    without standing up an invocation.
    """
    rendered = INSTRUCTION
    for key, value in state.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


__all__ = [
    "INSTRUCTION",
    "PROJECT_COACH_AGENT_NAME",
    "build_project_coach",
    "static_instruction",
]
