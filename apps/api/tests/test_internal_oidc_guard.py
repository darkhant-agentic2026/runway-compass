"""`/internal/*` skips OIDC only under `ENV=local`, and is closed everywhere else.

The third deliberate local-only surface in this project, on the same terms as
`Bearer dev:<uid>` (`test_auth_local_bypass.py`) and the local PUT receiver
(`test_local_storage_guard.py`): one `settings.is_local` check, and a named regression test
parametrized over every other `ENV`.

**Its failure mode is silent success**, which is why it gets a test rather than a comment.
An unguarded `/internal/tick` is an unauthenticated endpoint that spends money on somebody
else's behalf, and nothing about the service would look unhealthy while it happened — the
runs it creates are exactly the runs it is supposed to create.

The routes exist in every environment, unlike the local PUT receiver: Cloud Scheduler and
Cloud Tasks are the callers, and they are *only* real when deployed. So what this asserts
is the **verification**, not the registration.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from coach.core.config import Settings
from coach.main import create_app

DEPLOYED_ENVS = ("dev", "prod")

#: What a deployed environment needs before `Settings` will validate at all.
DEPLOYED = {
    "artifact_bucket": "coach-dev-artifacts",
    "upload_bucket": "coach-dev-uploads",
    "tasks_queue": "projects/p/locations/l/queues/q",
    "tasks_target_url": "https://coach-dev.example",
    "tasks_invoker_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_scheduler_sa": "coach-scheduler-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_tasks_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "oauth_client_id": "1234.apps.googleusercontent.com",
}

INTERNAL_PATHS = ("/internal/tick", "/internal/runs/r_01/execute")


def _deployed_app(env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A deployed-shaped app. Same two accommodations as `test_local_storage_guard.py`.

    `Settings` refuses to build with `FIRESTORE_EMULATOR_HOST` set for any non-local `ENV`
    — a guard of its own — and the suite exports that variable for every test. And outside
    `ENV=local` the SPA mount is unconditional, so `static/assets` has to exist: that is the
    documented early-failure behaviour (docs/07-infra-deploy.md#container), not something to
    work around.
    """
    (tmp_path / "static" / "assets").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    return create_app(
        Settings(
            env=env,  # type: ignore[arg-type]
            google_cloud_project="coach-dev",
            model_backend="vertex",
            log_level="WARNING",
            firestore_emulator_host=None,
            **DEPLOYED,
        )
    )


@pytest.mark.parametrize("env", DEPLOYED_ENVS)
@pytest.mark.parametrize("path", INTERNAL_PATHS)
async def test_a_deployed_internal_endpoint_refuses_an_unauthenticated_call(
    env: str, path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _deployed_app(env, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(path)

    assert response.status_code == 401
    # Reaching the app at all, rather than being answered by the SPA catch-all: a 404 here
    # would mean the route was missing, which is a different (and also wrong) world.
    assert response.json()["title"] != "Not found"


@pytest.mark.parametrize("env", DEPLOYED_ENVS)
@pytest.mark.parametrize("path", INTERNAL_PATHS)
async def test_a_deployed_internal_endpoint_refuses_a_bogus_token(
    env: str, path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bearer token that is not a Google OIDC token is rejected before any work happens.

    `verify_oauth2_token` reaches the network for Google's signing keys, and this token
    cannot get past the parse — so the assertion is on the refusal, not on the round trip.
    """
    app = _deployed_app(env, tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(path, headers={"Authorization": "Bearer not-a-token"})

    assert response.status_code == 401


@pytest.mark.parametrize("path", INTERNAL_PATHS)
async def test_the_local_bypass_is_reachable(path: str, client: httpx.AsyncClient) -> None:
    """The surface this guard protects, working.

    Without it `dev.sh tick` cannot reach the tick and the whole autonomous path is
    unexercisable on a laptop and by the e2e suite
    (docs/05-autonomous-runs.md#local-development). A test that only asserted the negative
    would keep passing if somebody removed the bypass and broke local development.

    The executor path answers `404` for a run id that does not exist, which is still the
    handler answering rather than the auth layer refusing.
    """
    response = await client.post(path)

    assert response.status_code in {200, 404}


def test_the_two_service_accounts_are_verified_separately() -> None:
    """Collapsing them would let Cloud Scheduler invoke the executor, or the reverse.

    Asserted by inspection rather than by driving a token, because the whole point is
    *which setting each endpoint reads* — and both would pass a test that only checked
    that some allow-list was consulted.
    """
    from coach.api.routers.internal import require_scheduler, require_tasks

    assert require_scheduler._setting_name == "allowed_scheduler_sa"
    assert require_tasks._setting_name == "allowed_tasks_sa"
    assert require_scheduler._setting_name != require_tasks._setting_name
