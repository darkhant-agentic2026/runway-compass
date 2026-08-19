"""Preference resolution.

docs/02-data-model.md:

    Preference resolution is a pure function, `resolve_prefs(global, project) ->
    EffectivePrefs`, used identically by the API, the UI (via
    `GET /api/projects/{id}/effective-prefs`), and the agent's prompt builder.

Pure and dependency-free on purpose: it is the one place the "global 45 min, project 2 h"
rule is decided, and it is cheap to exhaustively test (docs/08-testing.md asks for the
full inherit/override matrix).
"""

from __future__ import annotations

from coach.services.models import EffectivePrefs, GlobalPrefs, ProjectPrefs, ResearchDepth

#: Defaults for the project-scoped preferences that have no global counterpart. A `None`
#: on one of these means "unset", not "inherit", because there is nothing to inherit from.
DEFAULT_RESEARCH_DEPTH: ResearchDepth = "standard"
DEFAULT_ALLOW_VIDEOS = True
#: The gate on `complete_task_item` is on unless a project turns it off. Completing the
#: last step completes the task, so this default is what keeps docs/10-risks.md Q1 —
#: "completion is the learner's click" — true without the learner having to opt in.
DEFAULT_CONFIRM_ITEM_COMPLETION = True


def resolve_prefs(
    global_prefs: GlobalPrefs,
    project_prefs: ProjectPrefs | None = None,
) -> EffectivePrefs:
    """Merge global and project preferences into the values everything actually uses.

    A `None` on a project field means inherit; any other value — including `False` and
    `0`-adjacent values — overrides. That distinction is why the project model uses
    `None` rather than falsy sentinels: `allowVideos: false` is a real override.
    """
    project = project_prefs or ProjectPrefs()

    def inherited[T](override: T | None, fallback: T) -> T:
        return fallback if override is None else override

    return EffectivePrefs(
        default_task_minutes=inherited(
            project.default_task_minutes, global_prefs.default_task_minutes
        ),
        guidance_style=inherited(project.guidance_style, global_prefs.guidance_style),
        # Not overridable per project: the project prefs schema in docs/02-data-model.md
        # carries neither of these.
        verbosity=global_prefs.verbosity,
        timezone=global_prefs.timezone,
        research_depth=inherited(project.research_depth, DEFAULT_RESEARCH_DEPTH),
        allow_videos=inherited(project.allow_videos, DEFAULT_ALLOW_VIDEOS),
        confirm_item_completion=inherited(
            project.confirm_item_completion, DEFAULT_CONFIRM_ITEM_COMPLETION
        ),
        preferred_sources=inherited(project.preferred_sources, []),
        avoid_sources=inherited(project.avoid_sources, []),
    )
