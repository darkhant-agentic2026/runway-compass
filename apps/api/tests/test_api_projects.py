"""Project endpoints, over HTTP.

Per-user isolation is enforced entirely server-side (docs/02-data-model.md#access-model),
so the tests that matter most here are the ones where a second signed-in user tries to
reach the first user's data.
"""

from __future__ import annotations

import httpx


async def _create_project(client: httpx.AsyncClient, title: str = "Learn Rust") -> dict:
    response = await client.post("/api/projects", json={"title": title, "goal": "ship"})
    assert response.status_code == 201, response.text
    return response.json()


async def test_me_returns_the_verified_principal(client: httpx.AsyncClient) -> None:
    """M0's exit criterion, minus the deployment: the signed-in user sees their email."""
    response = await client.get("/api/me")
    assert response.status_code == 200
    body = response.json()
    assert body["uid"] == "u_alice"
    assert body["email"] == "u_alice@localhost.dev"
    assert body["globalPrefs"]["defaultTaskMinutes"] == 45
    assert body["plan"]["limits"]["autonomousRunsPerDay"] == 20


async def test_unauthenticated_requests_are_refused(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/api/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_patching_global_prefs_is_partial(client: httpx.AsyncClient) -> None:
    """Sending one key must not reset the others to their defaults."""
    await client.patch("/api/me/prefs", json={"timezone": "Europe/Berlin"})
    response = await client.patch("/api/me/prefs", json={"defaultTaskMinutes": 90})
    assert response.status_code == 200

    prefs = (await client.get("/api/me")).json()["globalPrefs"]
    assert prefs["defaultTaskMinutes"] == 90
    assert prefs["timezone"] == "Europe/Berlin"
    assert prefs["guidanceStyle"] == "socratic"


async def test_create_and_list_projects(client: httpx.AsyncClient) -> None:
    created = await _create_project(client)
    assert created["ownerUid"] == "u_alice"
    assert created["status"] == "active"
    assert created["counts"] == {"total": 0, "completed": 0, "openMinutes": 0}

    listed = (await client.get("/api/projects")).json()["projects"]
    assert [p["id"] for p in listed] == [created["id"]]


async def test_projects_are_isolated_per_user(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    mine = await _create_project(client, "Mine")
    theirs = await _create_project(other_client, "Theirs")

    my_list = (await client.get("/api/projects")).json()["projects"]
    assert [p["id"] for p in my_list] == [mine["id"]]

    their_list = (await other_client.get("/api/projects")).json()["projects"]
    assert [p["id"] for p in their_list] == [theirs["id"]]


async def test_another_users_project_is_not_found_rather_than_forbidden(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    """A distinguishable 403 would let any signed-in user probe for project ids."""
    mine = await _create_project(client)
    response = await other_client.get(f"/api/projects/{mine['id']}")
    assert response.status_code == 404


async def test_another_user_cannot_patch_or_archive(
    client: httpx.AsyncClient, other_client: httpx.AsyncClient
) -> None:
    mine = await _create_project(client)
    assert (
        await other_client.patch(f"/api/projects/{mine['id']}", json={"title": "pwned"})
    ).status_code == 404
    assert (await other_client.delete(f"/api/projects/{mine['id']}")).status_code == 404

    unchanged = (await client.get(f"/api/projects/{mine['id']}")).json()
    assert unchanged["title"] == "Learn Rust"
    assert unchanged["status"] == "active"


async def test_filtering_the_project_list_by_status(client: httpx.AsyncClient) -> None:
    active = await _create_project(client, "Active")
    archived = await _create_project(client, "Archived")
    await client.delete(f"/api/projects/{archived['id']}")

    listed = (await client.get("/api/projects?status=active")).json()["projects"]
    assert [p["id"] for p in listed] == [active["id"]]


async def test_delete_is_a_soft_archive(client: httpx.AsyncClient) -> None:
    project = await _create_project(client)
    response = await client.delete(f"/api/projects/{project['id']}")
    assert response.status_code == 200
    assert response.json()["status"] == "archived"
    # Still readable — nothing is destroyed.
    assert (await client.get(f"/api/projects/{project['id']}")).status_code == 200


async def test_effective_prefs_resolve_global_and_project(
    client: httpx.AsyncClient,
) -> None:
    """The brief's example, end to end over HTTP: global 45 min, project 2 h."""
    project = await _create_project(client)
    await client.patch("/api/me/prefs", json={"defaultTaskMinutes": 45})

    before = (await client.get(f"/api/projects/{project['id']}/effective-prefs")).json()
    assert before["effectivePrefs"]["defaultTaskMinutes"] == 45

    await client.patch(
        f"/api/projects/{project['id']}", json={"prefs": {"defaultTaskMinutes": 120}}
    )
    after = (await client.get(f"/api/projects/{project['id']}/effective-prefs")).json()
    assert after["effectivePrefs"]["defaultTaskMinutes"] == 120
    # Untouched preferences still come from the global layer.
    assert after["effectivePrefs"]["guidanceStyle"] == "socratic"


async def test_project_prefs_patch_is_whitelisted(client: httpx.AsyncClient) -> None:
    """An unknown key is refused rather than written onto the project document."""
    project = await _create_project(client)
    response = await client.patch(
        f"/api/projects/{project['id']}", json={"prefs": {"ownerUid": "u_mallory"}}
    )
    assert response.status_code == 422


async def test_a_project_pref_patch_does_not_clobber_siblings(
    client: httpx.AsyncClient,
) -> None:
    project = await _create_project(client)
    await client.patch(
        f"/api/projects/{project['id']}", json={"prefs": {"researchDepth": "deep"}}
    )
    await client.patch(f"/api/projects/{project['id']}", json={"prefs": {"allowVideos": False}})
    prefs = (await client.get(f"/api/projects/{project['id']}/effective-prefs")).json()[
        "effectivePrefs"
    ]
    assert prefs["researchDepth"] == "deep"
    assert prefs["allowVideos"] is False


async def test_healthz_needs_no_authentication(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        assert (await c.get("/healthz")).json() == {"status": "ok"}


async def test_readyz_reports_firestore_reachability(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        response = await c.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
