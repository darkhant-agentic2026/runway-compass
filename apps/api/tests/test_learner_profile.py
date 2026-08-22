"""Tests for the learner profile, adaptation tools, and memory integration (M7).

docs/09-roadmap.md#m7--learner-model-and-adaptation-1-week:
> - CoachMemoryService + contract suite; load_memory wired into task teacher and coach
> - update_learner_profile typed tool with versioning and audit trail (1 call/turn)
> - remember tool for durable memory entries
> - "What your coach knows about you" API and Settings support
"""

from __future__ import annotations

import httpx
from google.adk.tools.tool_context import ToolContext

from coach.agents.context import (
    DEFAULT_MINUTES_KEY,
    PROJECT_ID_KEY,
    TASK_ID_KEY,
)
from coach.agents.tools import DomainTools


class _FakeSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


class _FakeInvocationContext:
    def __init__(self, app_name: str, user_id: str, memory_service) -> None:
        self.app_name = app_name
        self.user_id = user_id
        self.memory_service = memory_service


class _FakeToolContext(ToolContext):
    def __init__(
        self,
        uid: str,
        session_id: str,
        project_id: str,
        task_id: str | None = None,
        memory_service=None,
    ) -> None:
        self._user_id = uid
        self._session = _FakeSession(session_id)
        self._state = {
            PROJECT_ID_KEY: project_id,
            TASK_ID_KEY: task_id or "",
            DEFAULT_MINUTES_KEY: 45,
        }
        self._invocation_context = _FakeInvocationContext("coach", uid, memory_service)

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def session(self) -> _FakeSession:
        return self._session

    @property
    def state(self) -> dict:
        return self._state

    @property
    def invocation_context(self) -> _FakeInvocationContext:
        return self._invocation_context


# --- update_learner_profile tool ------------------------------------------------------


async def test_agent_can_update_learner_profile(
    container, alice, client: httpx.AsyncClient
) -> None:
    tools: DomainTools = container.domain_tools
    project = (await client.post("/api/projects", json={"title": "ML Study"})).json()
    tool_ctx = _FakeToolContext(alice.uid, "s_session_1", project["id"])

    result = await tools.update_learner_profile(
        thinking_style="Bottom-up thinker; prefers working code examples first",
        strengths=["Python fundamentals", "Linear algebra"],
        gaps=["Backpropagation calculus"],
        technologies=[
            {"name": "PyTorch", "level": "intermediate", "evidence": "Built CNNs in PyTorch"},
        ],
        pacing="Fast, likes dense reference material",
        feedback_note="Understood gradient descent quickly after the code walkthrough",
        tool_context=tool_ctx,
    )

    assert result["ok"] is True
    profile = result["learnerProfile"]
    assert profile["thinkingStyle"] == "Bottom-up thinker; prefers working code examples first"
    assert profile["strengths"] == ["Python fundamentals", "Linear algebra"]
    assert profile["gaps"] == ["Backpropagation calculus"]
    assert profile["technologies"][0]["name"] == "PyTorch"
    assert profile["pacing"] == "Fast, likes dense reference material"
    assert profile["updatedBy"] == "agent"
    assert profile["version"] == 1
    assert any("[s_session_1]" in note for note in profile["feedbackNotes"])

    # Verify reflected in GET /api/me
    me = (await client.get("/api/me")).json()
    assert me["learnerProfile"]["version"] == 1
    assert (
        me["learnerProfile"]["thinkingStyle"]
        == "Bottom-up thinker; prefers working code examples first"
    )


async def test_update_learner_profile_is_rate_limited_to_one_call_per_turn(
    container, alice, client: httpx.AsyncClient
) -> None:
    """Rate-limited to 1 call/turn (docs/03-agent-design.md#memory-tools)."""
    tools: DomainTools = container.domain_tools
    project = (await client.post("/api/projects", json={"title": "Algorithms"})).json()
    tool_ctx = _FakeToolContext(alice.uid, "s_session_2", project["id"])

    first = await tools.update_learner_profile(
        thinking_style="Visual learner",
        tool_context=tool_ctx,
    )
    assert first["ok"] is True

    # Second call in the same turn (same tool_context.state)
    second = await tools.update_learner_profile(
        thinking_style="Auditory learner",
        tool_context=tool_ctx,
    )
    assert second["ok"] is False
    assert "already been updated" in second["error"]["message"]


# --- remember tool --------------------------------------------------------------------


async def test_remember_tool_stores_durable_memory(
    container, alice, client: httpx.AsyncClient
) -> None:
    tools: DomainTools = container.domain_tools
    project = (await client.post("/api/projects", json={"title": "Systems"})).json()
    tool_ctx = _FakeToolContext(
        alice.uid,
        "s_session_3",
        project["id"],
        memory_service=container.memory_service,
    )

    result = await tools.remember(
        text="Learner solved the deadlock bug by adopting strict lock ordering.",
        tags=["concurrency", "deadlocks"],
        tool_context=tool_ctx,
    )
    assert result["ok"] is True
    assert "deadlock" in result["remembered"]
    assert result["tags"] == ["concurrency", "deadlocks"]

    # Verify retrievable via memory_service
    search_resp = await container.memory_service.search_memory(
        app_name="coach",
        user_id=alice.uid,
        query="deadlock concurrency",
    )
    assert len(search_resp.memories) >= 1
    texts = [
        " ".join([p.text for p in m.content.parts if p.text])
        for m in search_resp.memories
        if m.content and m.content.parts
    ]
    assert any("lock ordering" in t for t in texts)


# --- PATCH /api/me/learner-profile (user edit & reset) --------------------------------


async def test_user_can_edit_and_reset_learner_profile(client: httpx.AsyncClient) -> None:
    # 1. Edit fields
    patch_res = await client.patch(
        "/api/me/learner-profile",
        json={
            "thinkingStyle": "Conceptual first, then code",
            "strengths": ["Architecture design"],
            "gaps": ["CSS animations"],
            "technologies": [
                {"name": "TypeScript", "level": "advanced", "evidence": "3 years fulltime"},
            ],
            "pacing": "Moderate",
        },
    )
    assert patch_res.status_code == 200
    profile = patch_res.json()["learnerProfile"]
    assert profile["thinkingStyle"] == "Conceptual first, then code"
    assert profile["strengths"] == ["Architecture design"]
    assert profile["technologies"][0]["name"] == "TypeScript"
    assert profile["updatedBy"] == "user"
    assert profile["version"] >= 1

    # 2. Reset specific fields
    reset_res = await client.patch(
        "/api/me/learner-profile",
        json={
            "thinkingStyle": "",
            "gaps": [],
        },
    )
    assert reset_res.status_code == 200
    reset_profile = reset_res.json()["learnerProfile"]
    assert reset_profile["thinkingStyle"] == ""
    assert reset_profile["gaps"] == []
    assert reset_profile["strengths"] == ["Architecture design"]  # untouched
    assert reset_profile["version"] == profile["version"] + 1
