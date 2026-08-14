"""`resolve_prefs` — the full inherit/override matrix (docs/08-testing.md)."""

from __future__ import annotations

import pytest

from coach.services.models import GlobalPrefs, ProjectPrefs
from coach.services.prefs import (
    DEFAULT_ALLOW_VIDEOS,
    DEFAULT_RESEARCH_DEPTH,
    resolve_prefs,
)


def test_the_briefs_example_global_45_project_120() -> None:
    """The example from the brief, spelled out because it is the requirement.

    "a 45-minute default task length globally, 2 hours in the project where you want it"
    """
    effective = resolve_prefs(
        GlobalPrefs(default_task_minutes=45),
        ProjectPrefs(default_task_minutes=120),
    )
    assert effective.default_task_minutes == 120


def test_a_project_with_no_preferences_inherits_everything() -> None:
    global_prefs = GlobalPrefs(
        default_task_minutes=30,
        guidance_style="direct",
        verbosity="terse",
        timezone="Europe/Berlin",
    )
    effective = resolve_prefs(global_prefs, ProjectPrefs())
    assert effective.default_task_minutes == 30
    assert effective.guidance_style == "direct"
    assert effective.verbosity == "terse"
    assert effective.timezone == "Europe/Berlin"


def test_no_project_at_all_behaves_like_an_empty_one() -> None:
    global_prefs = GlobalPrefs(default_task_minutes=90)
    assert resolve_prefs(global_prefs, None) == resolve_prefs(global_prefs, ProjectPrefs())


@pytest.mark.parametrize("style", ["socratic", "direct", "mixed"])
def test_guidance_style_overrides(style: str) -> None:
    effective = resolve_prefs(
        GlobalPrefs(guidance_style="socratic"),
        ProjectPrefs(guidance_style=style),  # type: ignore[arg-type]
    )
    assert effective.guidance_style == style


def test_project_only_fields_fall_back_to_their_documented_defaults() -> None:
    """`researchDepth` and `allowVideos` have no global counterpart.

    `None` on those means "unset", not "inherit" — there is nothing to inherit from.
    """
    effective = resolve_prefs(GlobalPrefs(), ProjectPrefs())
    assert effective.research_depth == DEFAULT_RESEARCH_DEPTH
    assert effective.allow_videos == DEFAULT_ALLOW_VIDEOS
    assert effective.preferred_sources == []
    assert effective.avoid_sources == []


def test_allow_videos_false_is_an_override_not_an_absence() -> None:
    """The reason project prefs use `None` rather than falsy sentinels.

    `allowVideos: false` has to survive resolution; if "falsy means inherit" crept in,
    a user who turned videos off would silently get them back.
    """
    effective = resolve_prefs(GlobalPrefs(), ProjectPrefs(allow_videos=False))
    assert effective.allow_videos is False


def test_empty_source_lists_are_an_override_not_an_absence() -> None:
    effective = resolve_prefs(
        GlobalPrefs(), ProjectPrefs(preferred_sources=[], avoid_sources=["medium.com"])
    )
    assert effective.preferred_sources == []
    assert effective.avoid_sources == ["medium.com"]


def test_verbosity_and_timezone_are_not_project_overridable() -> None:
    """The project prefs schema in docs/02-data-model.md carries neither field."""
    assert "verbosity" not in ProjectPrefs.model_fields
    assert "timezone" not in ProjectPrefs.model_fields
    effective = resolve_prefs(GlobalPrefs(verbosity="thorough", timezone="Asia/Tokyo"))
    assert effective.verbosity == "thorough"
    assert effective.timezone == "Asia/Tokyo"


def test_resolution_is_pure() -> None:
    """Nothing is mutated, so the same inputs always resolve the same way."""
    global_prefs = GlobalPrefs(default_task_minutes=45)
    project_prefs = ProjectPrefs(default_task_minutes=120)
    first = resolve_prefs(global_prefs, project_prefs)
    second = resolve_prefs(global_prefs, project_prefs)
    assert first == second
    assert global_prefs.default_task_minutes == 45
    assert project_prefs.default_task_minutes == 120


@pytest.mark.parametrize(
    ("global_minutes", "project_minutes", "expected"),
    [
        (45, None, 45),
        (45, 120, 120),
        (120, 45, 45),
        (15, 15, 15),
        (45, 1, 1),
    ],
)
def test_duration_matrix(
    global_minutes: int, project_minutes: int | None, expected: int
) -> None:
    effective = resolve_prefs(
        GlobalPrefs(default_task_minutes=global_minutes),
        ProjectPrefs(default_task_minutes=project_minutes),
    )
    assert effective.default_task_minutes == expected
