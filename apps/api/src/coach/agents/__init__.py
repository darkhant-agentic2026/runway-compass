"""ADK agent definitions, tools, callbacks, and prompts.

docs/03-agent-design.md#agent-graph. M2 built only the top-left box of that graph — a
single toolless coach agent — because the milestone's risk was in the streaming and
resume machinery, not in what the model could do. Domain tools landed at M3, research at
M4, and M6 split that one interactive agent into `project_coach` (the board as a whole)
and `task_teacher` (one task's checklist), so that a learner describing extra work for
the task in front of them cannot land on the board instead — see
docs/09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher.
"""

from __future__ import annotations

from coach.agents.project_coach import PROJECT_COACH_AGENT_NAME, build_project_coach
from coach.agents.runner import RunnerFactory
from coach.agents.task_teacher import TASK_TEACHER_AGENT_NAME, build_task_teacher

__all__ = [
    "PROJECT_COACH_AGENT_NAME",
    "TASK_TEACHER_AGENT_NAME",
    "RunnerFactory",
    "build_project_coach",
    "build_task_teacher",
]
