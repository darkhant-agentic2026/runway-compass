"""`coach_agent` — the conversation the learner has while working on a task.

docs/03-agent-design.md#coach_agent. The behaviours below are the ones that document
lists; the clauses that depend on machinery this milestone does not have yet are marked,
so the instruction grows by deletion of those markers rather than by rewrite.

`thinking_level=high`, per the agent graph: this is the Socratic conversation, which is
the expensive-thinking half of the system rather than the mechanical one.

**No tools at M2.** The agent graph gives `coach_agent` domain tools, memory tools, and
`AgentTool(research_agent)`; all three arrive later (M3, M6, M4). Adding an empty `tools`
list now would also be the wrong shape for a different reason — `LlmAgent.canonical_tools`
treats `len(tools) > 1` as the trigger for built-in-tool wrapping
(docs/03-agent-design.md#m1-spike-result-resolved-against-the-installed-270-source), so
the tool list is something to grow deliberately.
"""

from __future__ import annotations

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm

from coach.integrations.model import generation_config

COACH_AGENT_NAME = "coach_agent"

INSTRUCTION = """\
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

Answer in Markdown. Prefer short paragraphs and concrete next steps over exhaustive
explanation.
"""


def build_coach_agent(model: str | BaseLlm) -> LlmAgent:
    """The interactive coach.

    `model` is passed in rather than built here so that the disconnect suite can drive a
    scripted fake and the deployed service can drive Gemini through exactly the same
    agent — the streaming path must not have a test-only variant, since the streaming
    path is what this milestone exists to get right.
    """
    return LlmAgent(
        name=COACH_AGENT_NAME,
        model=model,
        description="Socratic study coach for one learner's task.",
        instruction=INSTRUCTION,
        generate_content_config=generation_config("high"),
    )


__all__ = ["COACH_AGENT_NAME", "INSTRUCTION", "build_coach_agent"]
