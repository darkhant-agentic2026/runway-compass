"""`POST /api/projects/{id}/session` — the project's intake conversation.

docs/04-api-contract.md has `POST /api/projects` create an intake session and nothing
that finds it again; this endpoint is the M3 addition that closes that gap
(docs/09-roadmap.md records it as a deviation).

The interesting part is not the happy path but the three sources it resolves from, in
cost order — the pointer, the collection-group scan, then creation — because the middle
one exists only for projects created before the pointer did, and an untested fallback is
a fallback that will be wrong the first time it runs.
"""

from __future__ import annotations

import httpx

from coach.adk_firestore.session_service import PROJECT_ID_FIELD, TASK_ID_FIELD
from coach.core.app import APP_NAME
from coach.core.principal import Principal


async def test_creating_a_project_records_its_intake_session(
    client: httpx.AsyncClient,
) -> None:
    project = (await client.post("/api/projects", json={"title": "Learn Rust"})).json()
    assert project["intakeSessionId"]

    opened = await client.post(f"/api/projects/{project['id']}/session")
    assert opened.status_code == 200
    assert opened.json()["id"] == project["intakeSessionId"]
    assert opened.json()["taskId"] is None


async def test_reopening_returns_the_same_session(client: httpx.AsyncClient) -> None:
    """Get-or-create, so a reload does not fork the conversation."""
    project = (await client.post("/api/projects", json={"title": "Learn Rust"})).json()
    first = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]
    second = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]
    assert first == second


async def test_a_project_without_the_pointer_is_repaired_by_the_scan(
    client: httpx.AsyncClient, container
) -> None:
    """The M2-era project: an intake session exists, nothing points at it.

    The pointer is cleared rather than the session deleted, which is exactly the shape of
    a project created before M3. The scan has to find it — creating a second intake
    session would strand the first conversation with no way back to it — and then write
    the pointer, so the scan is paid once.
    """
    project = (await client.post("/api/projects", json={"title": "Legacy"})).json()
    original = project["intakeSessionId"]
    await container.project_repository.patch(project["id"], {"intakeSessionId": None})

    found = (await client.post(f"/api/projects/{project['id']}/session")).json()["id"]

    assert found == original
    refreshed = (await client.get(f"/api/projects/{project['id']}")).json()
    assert refreshed["intakeSessionId"] == original


async def test_the_scan_ignores_task_sessions(client: httpx.AsyncClient, container) -> None:
    """`taskId is None` is the whole definition of an intake session.

    The filter runs in Python rather than as a second `where`, because a two-filter
    collection-group query needs a composite index that the emulator does not ask for and
    Firestore does (docs/09-roadmap.md). So the Python half is what is under test: give
    the project a task session, drop the pointer, and the scan must still pick the right
    one out of the two.
    """
    project = (await client.post("/api/projects", json={"title": "Two sessions"})).json()
    intake = project["intakeSessionId"]
    task = (
        await client.post(
            f"/api/projects/{project['id']}/tasks",
            json={"title": "Read the book", "estimatedMinutes": 45},
        )
    ).json()["task"]
    await client.post(f"/api/tasks/{task['id']}/session")
    await container.project_repository.patch(project["id"], {"intakeSessionId": None})

    assert (await client.post(f"/api/projects/{project['id']}/session")).json()["id"] == intake


async def test_a_stale_pointer_falls_through_rather_than_404ing(
    client: httpx.AsyncClient, container
) -> None:
    """A pointer to a session that no longer exists is a cache miss, not an error."""
    project = (await client.post("/api/projects", json={"title": "Stale"})).json()
    await container.project_repository.patch(project["id"], {"intakeSessionId": "s_deleted"})

    response = await client.post(f"/api/projects/{project['id']}/session")
    assert response.status_code == 200
    assert response.json()["id"] == project["intakeSessionId"]


async def test_another_user_cannot_open_it(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    """`NotFound`, not `Forbidden` — project ids must not be probeable."""
    project = (await client.post("/api/projects", json={"title": "Private"})).json()
    response = await other_client.post(f"/api/projects/{project['id']}/session")
    assert response.status_code == 404


async def test_the_linkage_is_on_the_session_document(
    client: httpx.AsyncClient, container, alice: Principal
) -> None:
    """What the scan filters on, asserted at the document rather than through the API."""
    project = (await client.post("/api/projects", json={"title": "Linkage"})).json()
    session_id = project["intakeSessionId"]

    reference = container.session_service._get_sessions_ref(APP_NAME, alice.uid).document(
        session_id
    )
    data = (await reference.get()).to_dict() or {}
    assert data[PROJECT_ID_FIELD] == project["id"]
    assert data[TASK_ID_FIELD] is None
