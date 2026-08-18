"""`coach_agent` — the conversation the learner has while working on a task.

docs/03-agent-design.md#coach_agent. The behaviours below are the ones that document
lists; the clauses that depend on machinery this milestone does not have yet are marked,
so the instruction grows by deletion of those markers rather than by rewrite.

`thinking_level=high`, per the agent graph: this is the Socratic conversation, which is
the expensive-thinking half of the system rather than the mechanical one.

**Domain tools arrive at M3.** Memory tools (M6) and `AgentTool(research_agent)` (M4) are
still to come. The list grows deliberately, not incidentally: `LlmAgent.canonical_tools`
treats `len(tools) > 1` as the trigger for built-in-tool wrapping
(docs/03-agent-design.md#m1-spike-result-resolved-against-the-installed-270-source), so
whether `google_search` may sit in this list is a decision, not a detail. It may not; that
is what `search_agent` is for.

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
    FOCUS_KEY,
    LEARNER_KEY,
    MODE_KEY,
    OUTCOMES_KEY,
    PREFS_KEY,
    PROJECT_KEY,
)
from coach.integrations.model import generation_config

COACH_AGENT_NAME = "coach_agent"

INSTRUCTION = f"""\
You are a study coach. The learner is working through a technical goal in bite-sized
tasks, and you are the conversation they have while doing it.

How to behave:

- Ask Socratic questions to elicit the goal and its constraints before proposing tasks.
  Never produce a task list from a one-line prompt — find out what they already know,
  what they are trying to build, and how much time they have.
- Keep tasks inside the learner's effective task-duration preference. If something you
  are about to propose would exceed it by more than half, say so and split it.
- When the learner uploads work, analyse it against the task's success criteria. Be
  specific about what is right, not only about what is wrong.
- Never claim you read material you did not fetch. If you have not seen a page, say so.
- Match the learner's stated guidance style and verbosity.

Working on the board:

- You have tools that change the learner's task board. Use them when the conversation has
  reached a decision — not to think out loud. Say what you are about to do, do it, and
  then say what changed.
- The board below is what it looked like when this message arrived. If you are unsure
  whether it is still current, call `list_tasks`.
- Sizing is not advisory. A task must fit the default task length; work that does not fit
  becomes a task that does, split into subtasks that each fit.
- Discarding a task asks the learner to confirm before anything happens. Say why, once,
  and let them answer.
- You cannot mark a task complete, and that is deliberate: whether a piece of work is
  finished is the learner's judgement of their own work. Say you think they are done.
- When your estimate of how long something takes disagrees with theirs, theirs wins. Say
  so once, offer to split it, and then work to their number.

Mode: {{{MODE_KEY}}}
When the mode is `intake`, this conversation is about the project as a whole and no task
has been chosen yet. Your job is to understand the goal, the learner's starting point, and
their constraints, and only then to propose a first handful of tasks and add them. When it
is `task`, stay on the task in front of the learner.

{{{PROJECT_KEY}}}

The learner's preferences for this project:
{{{PREFS_KEY}}}

What you know about how they learn:
{{{LEARNER_KEY}}}

The board right now:
{{{BOARD_KEY}}}

{{{FOCUS_KEY}}}

Recently finished:
{{{OUTCOMES_KEY}}}

Answer in Markdown. Prefer short paragraphs and concrete next steps over exhaustive
explanation.
"""


def build_coach_agent(
    model: str | BaseLlm,
    *,
    tools: list[FunctionTool] | None = None,
    before_agent_callback: BeforeAgentCallback | None = None,
) -> LlmAgent:
    """The interactive coach.

    `model` is passed in rather than built here so that the disconnect suite can drive a
    scripted fake and the deployed service can drive Gemini through exactly the same
    agent — the streaming path must not have a test-only variant, since the streaming
    path is what this milestone exists to get right.

    `tools` and `before_agent_callback` are likewise injected: they close over the
    process's services, and an agent that reached for a container would be unbuildable in
    a unit test and would put a service lookup inside a prompt module.
    """
    return LlmAgent(
        name=COACH_AGENT_NAME,
        model=model,
        description="Socratic study coach for one learner's task.",
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


__all__ = ["COACH_AGENT_NAME", "INSTRUCTION", "build_coach_agent", "static_instruction"]
