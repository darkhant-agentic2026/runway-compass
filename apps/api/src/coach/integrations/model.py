"""The model backend, behind one switch.

docs/00-overview.md#model-configuration: primary model `gemini-3.7-flash`, via **Vertex
AI** in production (IAM-based auth, no API key to rotate) and the **Gemini API** for
local development (fastest onboarding). One abstraction, selected by `MODEL_BACKEND`.

**Gemini 3.x changed generation config in ways that break older snippets.** The
`GenerateContentConfig` built here therefore sends none of the parameters that a
pre-3.x example would reach for:

- `temperature`, `top_p`, `top_k` are **not** sent.
- `thinking_level` (`low | medium | high`) replaces `thinking_budget`. Default `medium`;
  `low` for mechanical tool steps (task splitting, reordering — M3) and `high` for
  research synthesis and the Socratic intake conversation.
- `candidate_count` is unsupported.

Adding any of them back is not a tuning decision, it is an API error waiting for the
first real request — which is why they are named here rather than merely omitted.
"""

from __future__ import annotations

from typing import Literal

from google.adk.models.base_llm import BaseLlm
from google.adk.models.google_llm import Gemini
from google.genai import types

from coach.core.config import Settings

ThinkingLevel = Literal["low", "medium", "high"]

_THINKING_LEVELS: dict[ThinkingLevel, types.ThinkingLevel] = {
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}


def generation_config(thinking_level: ThinkingLevel = "medium") -> types.GenerateContentConfig:
    """The only place a `GenerateContentConfig` is built. See the module docstring."""
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level=_THINKING_LEVELS[thinking_level]),
    )


def build_model(settings: Settings) -> BaseLlm:
    """The configured model, constructed but not yet connected.

    `Gemini` resolves its `google.genai.Client` lazily through a cached property, so
    building one costs nothing and — importantly — does not resolve credentials. That is
    what lets the app start, serve `/livez`, and run its tests without a model backend
    being reachable.
    """
    if settings.model_backend == "stub":
        # The end-to-end harness. `Settings` has already refused this for any non-local
        # `ENV`, so reaching here in a deployed environment is impossible.
        from coach.integrations.stub_model import StubModel

        return StubModel()
    if settings.model_backend == "gemini_api":
        # Local development. The key is validated by `Settings`, which refuses this
        # backend without one.
        return Gemini(
            model=settings.model_name,
            client_kwargs={"api_key": settings.gemini_api_key, "vertexai": False},
        )
    return Gemini(
        model=settings.model_name,
        client_kwargs={
            "vertexai": True,
            "project": settings.google_cloud_project,
            "location": settings.vertex_location,
        },
    )


__all__ = ["ThinkingLevel", "build_model", "generation_config"]
