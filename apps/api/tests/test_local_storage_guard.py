"""The local PUT receiver is inert outside `ENV=local`.

`api/routers/local_storage.py` accepts an **unauthenticated** PUT, because it stands in for
a signed URL whose capability *is* the URL. That is only defensible while the route cannot
exist anywhere real, so the guard gets the same treatment as the `Bearer dev:<uid>` path in
`test_auth_local_bypass.py`: a named regression test, parametrized over every other `ENV`,
rather than a comment and a hope.

Note what the negative case asserts. A missing route under `/api/*` is answered by the SPA
catch-all with a 404 `problem+json` (`main.API_PREFIXES`), so "not registered" and "not
found" look identical from outside — which is the point. The route table is checked
directly as well, so a 404 that happened to come from somewhere else could not pass this.
"""

from __future__ import annotations

import httpx
import pytest

from coach.api.routers.local_storage import PREFIX
from coach.main import create_app

DEPLOYED_ENVS = ("dev", "prod")

#: What a deployed environment needs before `Settings` will validate at all.
DEPLOYED = {
    "artifact_bucket": "coach-dev-artifacts",
    "upload_bucket": "coach-dev-uploads",
    "tasks_queue": "projects/p/locations/l/queues/q",
    "tasks_target_url": "https://coach-dev.example/internal/runs",
    "tasks_invoker_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_scheduler_sa": "coach-scheduler-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_tasks_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "oauth_client_id": "1234.apps.googleusercontent.com",
}


def _paths(app) -> set[str]:
    """Every path the app has a route for.

    FastAPI wraps `include_router` results in `_IncludedRouter`, which carries no `path` of
    its own — the real routes hang off `original_router`, so a naive walk of `app.routes`
    finds only the handful registered directly and would make the positive assertion below
    pass for the wrong reason.
    """
    found: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            found.add(path)
        for nested in getattr(getattr(route, "original_router", None), "routes", ()):
            nested_path = getattr(nested, "path", None)
            if isinstance(nested_path, str):
                found.add(nested_path)
    return found


def _deployed_app(settings, tmp_path, monkeypatch, env: str):
    """A `create_app` for a deployed `ENV`, with the cloud clients stubbed out.

    `static/assets` has to exist: outside `ENV=local` the SPA mount is unconditional, so a
    missing build fails the container at import time. That is the documented early-failure
    behaviour (docs/07-infra-deploy.md#container), not something to work around — so the
    directory is created rather than the mount made conditional.
    """
    (tmp_path / "static" / "assets").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    monkeypatch.setattr("coach.api.deps.build_object_store", lambda _s: object())
    monkeypatch.setattr("coach.api.deps.build_artifact_service", lambda _s: object())
    return create_app(
        settings.model_copy(update={"env": env, "firestore_emulator_host": None, **DEPLOYED})
    )


def test_the_route_exists_when_env_is_local(app) -> None:
    assert any(path.startswith(PREFIX) for path in _paths(app))


@pytest.mark.parametrize("env", DEPLOYED_ENVS)
def test_the_route_is_absent_for_every_other_env(
    env: str, settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _deployed_app(settings, tmp_path, monkeypatch, env)

    assert not any(path.startswith(PREFIX) for path in _paths(app))


@pytest.mark.parametrize("env", DEPLOYED_ENVS)
async def test_a_put_is_refused_for_every_other_env(
    env: str, settings, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _deployed_app(settings, tmp_path, monkeypatch, env)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.put(f"{PREFIX}/u_alice/up_1/shot.png", content=b"bytes")

    assert response.status_code in {404, 405}


async def test_a_local_put_records_what_was_actually_sent(client, container) -> None:
    """And that is what makes finalize's checks real rather than vacuous locally."""
    created = (
        await client.post(
            "/api/uploads",
            json={"filename": "shot.png", "mimeType": "image/png", "sizeBytes": 5},
        )
    ).json()
    record = await container.upload_repository.get(created["uploadId"])

    put = await client.put(
        created["signedUrl"], content=b"12345", headers={"Content-Type": "image/png"}
    )

    assert put.status_code == 200
    assert await container.object_store.stat(record["objectName"]) == (5, "image/png")
    assert await container.object_store.download(record["objectName"]) == b"12345"


async def test_the_signed_url_is_same_origin_so_a_browser_can_reach_it(client) -> None:
    """The previous `https://storage.local/…` placeholder is why uploads had no e2e test."""
    created = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 3},
        )
    ).json()

    assert created["signedUrl"].startswith(f"{PREFIX}/")


async def test_finalize_rejects_a_type_the_client_lied_about(client, container) -> None:
    """Real bytes make the MIME check meaningful: the PUT's content type is what counts."""
    created = (
        await client.post(
            "/api/uploads",
            json={"filename": "a.png", "mimeType": "image/png", "sizeBytes": 4},
        )
    ).json()
    await client.put(
        created["signedUrl"],
        content=b"MZ\x90\x00",
        headers={"Content-Type": "application/x-msdownload"},
    )

    response = await client.post(f"/api/uploads/{created['uploadId']}/finalize")

    assert response.status_code == 422
