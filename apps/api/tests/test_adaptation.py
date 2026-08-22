"""Multi-session learner adaptation and profile evolution tests (M7).

docs/09-roadmap.md#m7--learner-model-and-adaptation-1-week:
> Exit: across three sessions the coach demonstrably adapts (evalset check); every
> profile change is attributable to a session and reversible by the user.
"""

from __future__ import annotations

import httpx

from coach.agents.prompt import (
    LEARNER_KEY,
    PromptBuilder,
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


class _FakeToolContext:
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
            "temp:coach_project_id": project_id,
            "temp:coach_task_id": task_id or "",
            "temp:coach_default_minutes": 45,
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


async def test_multi_session_adaptation_cycle(
    container, alice, client: httpx.AsyncClient
) -> None:
    """Demonstrate end-to-end adaptation across three sessions:

    1. Session 1: Coach observes learner tendencies, updates profile, and remembers insights.
       Profile is versioned and attributed to session 1.
    2. Session 2: Prompt builder injects learned profile into the next session.
       Coach loads memory from session 1 and refines beliefs.
    3. Session 3: Learner inspects profile via /api/me and reverses / resets a belief.
       Subsequent prompts immediately reflect the user's reversal.
    """
    tools: DomainTools = container.domain_tools
    prompt_builder: PromptBuilder = container.prompt_builder

    # --- Session 1: Initial Discovery ------------------------------------------------
    proj_res = await client.post("/api/projects", json={"title": "Data Engineering"})
    project_id = proj_res.json()["id"]

    task1 = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Setup DuckDB", "estimatedMinutes": 45},
        )
    ).json()["task"]
    session1_resp = await client.post(f"/api/tasks/{task1['id']}/session")
    session1_id = session1_resp.json()["session"]["id"]

    tool_ctx_1 = _FakeToolContext(
        alice.uid, session1_id, project_id, task1["id"], memory_service=container.memory_service
    )

    # Coach observes learner preferences during session 1
    update_res = await tools.update_learner_profile(
        thinking_style="Learns best from hands-on SQL and concrete examples before theory",
        strengths=["SQL querying", "Relational schemas"],
        gaps=["Columnar storage internals"],
        technologies=[
            {
                "name": "DuckDB",
                "level": "beginner",
                "evidence": "First time trying in session 1",
            },
            {"name": "Postgres", "level": "advanced", "evidence": "5 years production DBA"},
        ],
        pacing="Fast-paced",
        feedback_note="Grasped OLAP vs OLTP quickly once contrasted with Postgres",
        tool_context=tool_ctx_1,  # type: ignore[arg-type]
    )
    assert update_res["ok"] is True
    profile1 = update_res["learnerProfile"]
    assert profile1["version"] == 1
    assert profile1["updatedBy"] == "agent"
    assert any(f"[{session1_id}]" in note for note in profile1["feedbackNotes"])

    # Coach stores episodic memory item
    rem_res = await tools.remember(
        text="Prefers CLI tools over GUI clients when testing queries.",
        tags=["preferences", "tools"],
        tool_context=tool_ctx_1,  # type: ignore[arg-type]
    )
    assert rem_res["ok"] is True

    # --- Session 2: Second Task, Prompt Ingestion & Memory Recall --------------------
    task2 = (
        await client.post(
            f"/api/projects/{project_id}/tasks",
            json={"title": "Parquet Partitioning", "estimatedMinutes": 60},
        )
    ).json()["task"]
    session2_resp = await client.post(f"/api/tasks/{task2['id']}/session")
    session2_id = session2_resp.json()["session"]["id"]

    # Prompt builder for Session 2 builds prompt state for alice
    class _CallbackContext:
        def __init__(self, uid: str, sid: str) -> None:
            self.user_id = uid
            self.session = type("_S", (), {"id": sid})()
            self.state: dict[str, object] = {}

    cb_ctx_2 = _CallbackContext(alice.uid, session2_id)
    await prompt_builder(callback_context=cb_ctx_2)  # type: ignore[arg-type]

    # Prompt state visibly carries what the coach learned in Session 1
    learner_prompt = str(cb_ctx_2.state[LEARNER_KEY])
    assert "Learns best from hands-on SQL" in learner_prompt
    assert "SQL querying, Relational schemas" in learner_prompt
    assert "Columnar storage internals" in learner_prompt
    assert "DuckDB (beginner)" in learner_prompt
    assert "Postgres (advanced)" in learner_prompt
    assert "Fast-paced" in learner_prompt

    # In Session 2, coach recalls memory stored in Session 1
    search_res = await container.memory_service.search_memory(
        app_name="coach",
        user_id=alice.uid,
        query="CLI tools preferences",
    )
    assert len(search_res.memories) >= 1
    assert any(
        "CLI tools over GUI" in " ".join([p.text for p in m.content.parts if p.text])
        for m in search_res.memories
        if m.content and m.content.parts
    )

    # Coach advances DuckDB skill in Session 2
    tool_ctx_2 = _FakeToolContext(
        alice.uid, session2_id, project_id, task2["id"], memory_service=container.memory_service
    )
    update_res_2 = await tools.update_learner_profile(
        technologies=[
            {
                "name": "DuckDB",
                "level": "intermediate",
                "evidence": "Mastered Parquet partitioning",
            },
            {"name": "Postgres", "level": "advanced", "evidence": "5 years production DBA"},
        ],
        feedback_note="Completed partitioning exercise with zero errors",
        tool_context=tool_ctx_2,  # type: ignore[arg-type]
    )
    assert update_res_2["ok"] is True
    profile2 = update_res_2["learnerProfile"]
    assert profile2["version"] == 2
    assert profile2["technologies"][0]["level"] == "intermediate"
    assert any(f"[{session2_id}]" in note for note in profile2["feedbackNotes"])

    # --- Session 3: User Inspection & Reversal in Settings ---------------------------
    # User fetches /api/me to see "What your coach knows about you"
    me_res = await client.get("/api/me")
    assert me_res.status_code == 200
    user_view = me_res.json()["learnerProfile"]
    assert user_view["version"] == 2
    assert len(user_view["feedbackNotes"]) == 2

    # User overrides/resets a belief (e.g. gaps cleared, thinking style edited)
    user_patch = await client.patch(
        "/api/me/learner-profile",
        json={
            "gaps": [],  # Learner feels columnar internals are no longer a gap
            "thinkingStyle": "Prefers architecture diagrams and formal specs first",
        },
    )
    assert user_patch.status_code == 200
    profile3 = user_patch.json()["learnerProfile"]
    assert profile3["version"] == 3
    assert profile3["updatedBy"] == "user"
    assert profile3["gaps"] == []
    assert profile3["thinkingStyle"] == "Prefers architecture diagrams and formal specs first"

    # Next session immediately reflects user's reversal
    cb_ctx_3 = _CallbackContext(alice.uid, session2_id)
    await prompt_builder(callback_context=cb_ctx_3)  # type: ignore[arg-type]
    learner_prompt_3 = str(cb_ctx_3.state[LEARNER_KEY])
    assert "Prefers architecture diagrams and formal specs first" in learner_prompt_3
    assert "Columnar storage internals" not in learner_prompt_3
