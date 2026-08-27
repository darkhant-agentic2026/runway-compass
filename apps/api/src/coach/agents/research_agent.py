"""`search_agent` — the built-in `google_search` hop, shared by the research pipeline.

docs/03-agent-design.md#agent-graph. A separate `LlmAgent` rather than a tool on the caller
directly, and the split is forced rather than chosen: Gemini refuses to mix the built-in
`google_search` tool with custom function tools in a single agent, so the built-in is
isolated here and exposed to callers through `AgentTool`.

**The M1 spike confirmed the restriction still holds on the pinned 2.7.0**, and that ADK's
own workaround is the same hop generated for you behind a non-default flag
(docs/03-agent-design.md#m1-spike-result-resolved-against-the-installed-270-source).
Option A — writing the hop ourselves — was chosen so the sub-agent's model, instruction,
and `thinking_level` stay ours; `create_google_search_agent` picks all three. `low`,
because it runs a query and reports what came back, and paying for deliberation there buys
nothing.

**Until M9 this module also built `research_agent`, the single LlmAgent that did all of a
research job's planning, searching, and writing.** M9 split that one agent into the
`research_workflow` pipeline (`agents/research_workflow.py`); `search_agent` is the one
piece of the old module still standing, now shared by that pipeline's `topic_researcher`
node the same way it was shared by `research_agent` before.
"""

from __future__ import annotations

from google.adk.agents.llm_agent import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.tools.google_search_tool import google_search

from coach.integrations.model import generation_config

SEARCH_AGENT_NAME = "search_agent"

SEARCH_INSTRUCTION = """\
You search the web and report what you found. You do not summarise, judge, or recommend.

For each result, give the title, the URL, and one line on what the page appears to cover,
taken from the search result itself. Say plainly when a query returns nothing useful — an
empty answer is more useful than a plausible one, because whatever you return will be
fetched and read.
"""


def build_search_agent(model: str | BaseLlm) -> LlmAgent:
    """The `google_search` hop. Its tool list is *only* the built-in, deliberately."""
    return LlmAgent(
        name=SEARCH_AGENT_NAME,
        model=model,
        description="Searches the web and reports results verbatim.",
        instruction=SEARCH_INSTRUCTION,
        generate_content_config=generation_config("low"),
        tools=[google_search],
    )


__all__ = [
    "SEARCH_AGENT_NAME",
    "SEARCH_INSTRUCTION",
    "build_search_agent",
]
