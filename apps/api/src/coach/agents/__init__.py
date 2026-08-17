"""ADK agent definitions, tools, callbacks, and prompts.

docs/03-agent-design.md#agent-graph. M2 builds only the top-left box of that graph — a
`coach_agent` with no tools at all — because the milestone's risk is in the streaming and
resume machinery, not in what the model can do. Domain tools land at M3, research at M4.

Keeping the agent deliberately toolless here is what lets the disconnect matrix
(docs/08-testing.md) be about disconnects: a scripted fake model produces a known delta
sequence, and nothing else in the loop can vary.
"""

from __future__ import annotations

from coach.agents.coach_agent import COACH_AGENT_NAME, build_coach_agent
from coach.agents.runner import RunnerFactory

__all__ = ["COACH_AGENT_NAME", "RunnerFactory", "build_coach_agent"]
