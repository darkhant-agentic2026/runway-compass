"""`propose_tasks` — the one LLM step of a run that is allowed to touch the board.

docs/03-agent-design.md#autonomous_workflow, step 4: "May emit `add_task` / `add_subtask`
calls if research revealed missing prerequisites. Bounded: ≤ 5 new tasks per run."

It is a separate agent from `coach_agent` rather than the same one with a different
message, and the difference is not cosmetic:

- **It runs with the reduced tool set** (`DomainTools.as_autonomous_tools`), so the safety
  rails are a property of the agent rather than of the instruction. A prompt that asks a
  model not to discard tasks is an honour system; an agent with no `discard_task` cannot.
- **Nobody is reading.** `coach_agent`'s instruction is written for a conversation — it
  asks questions, proposes, waits. Every one of those behaviours is wrong here, where the
  reply goes into a transcript the learner will find tomorrow. So the instruction says to
  act or to say nothing, and says it in the past tense.
- `thinking_level` is `low`, not `high`. docs/00-overview.md reserves `high` for research
  synthesis and the Socratic intake; this step is a bounded mechanical decision about
  whether one or two prerequisites are missing, which is exactly the kind the same document
  puts on `low`.

**The placeholders are load-bearing.** `{temp:coach_*}` is filled by `agents/prompt.py`,
and `inject_session_state` raises `KeyError` on a placeholder with no writer — inside the
detached generation task, on the first real run of a deployed revision. Only keys that
callback already writes appear below, and `tests/test_agent_prompt.py` reads this template
rather than restating its list.
"""

from __future__ import annotations

from google.adk.agents.base_agent import BeforeAgentCallback
from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.function_tool import FunctionTool

from coach.agents.prompt import BOARD_KEY, FOCUS_KEY, PREFS_KEY, PROJECT_KEY
from coach.integrations.model import generation_config

AUTONOMOUS_AGENT_NAME = "propose_tasks_agent"

INSTRUCTION = f"""\
You are a study coach working in the background, between the learner's sessions. They are
not here and will read this later.

You have just finished preparing the materials for one task. Your only job now is to
decide whether that research revealed work the board is **missing** — a prerequisite the
material assumes the learner already has, or a step this task turns out to need first.

How to decide:

- Add something only if a learner working from these materials would get stuck without it.
  A topic that is merely adjacent, interesting, or "worth knowing" is not missing work.
- At most two additions. This is a background pass, not a replanning session.
- Something that belongs *inside* the task in front of you is a subtask
  (`add_subtask`); something that is a separate sitting of its own goes on the board
  (`add_task`). Sizing rules apply as always — anything that does not fit the learner's
  default task length is two pieces of work.
- If the board already covers everything, add nothing and say so in one line. That is a
  good outcome and the common one.

How to write:

- Past tense, addressed to the learner, three sentences at most. They are reading a
  transcript, not having a conversation, so do not ask questions and do not offer choices —
  there is nobody to answer them, and an unanswered question sits in their session looking
  like you are waiting.
- Name what you added and why in the same sentence. "Added *Set up a virtualenv* first —
  the tutorial assumes one" is the whole of it.

{{{PROJECT_KEY}}}

The learner's preferences for this project:
{{{PREFS_KEY}}}

The board right now:
{{{BOARD_KEY}}}

{{{FOCUS_KEY}}}

Answer in Markdown.
"""


def build_autonomous_agent(
    model: str | BaseLlm,
    *,
    tools: list[FunctionTool] | None = None,
    before_agent_callback: BeforeAgentCallback | None = None,
) -> LlmAgent:
    """The `propose_tasks` agent.

    `tools` is injected exactly as `build_coach_agent`'s is, and the caller is expected to
    pass `DomainTools.as_autonomous_tools()`. It is not defaulted here: a builder that
    reached for the reduced set itself would make "which tools does background work have"
    a fact split across two files, and the one place it should live is the method that
    enumerates them.
    """
    return LlmAgent(
        name=AUTONOMOUS_AGENT_NAME,
        model=model,
        description="Background pass that adds prerequisites research turned up.",
        instruction=INSTRUCTION,
        generate_content_config=generation_config("low"),
        tools=list(tools or []),
        before_agent_callback=before_agent_callback,
    )


def static_instruction(state: dict[str, str]) -> str:
    """`INSTRUCTION` rendered against `state`, for the prompt test. See `coach_agent`."""
    rendered = INSTRUCTION
    for key, value in state.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


__all__ = [
    "AUTONOMOUS_AGENT_NAME",
    "INSTRUCTION",
    "build_autonomous_agent",
    "static_instruction",
]
