"""The `ENV=local` dev-token path, and the proof that it is inert everywhere else.

docs/08-testing.md:

    **`ENV=local` auth bypass is inert everywhere else.** Parametrized over every
    non-`local` `ENV` value: `Authorization: Bearer dev:someuid` must return `401`, and
    the dev branch must not be reachable. This is deliberate auth-bypass code, so it gets
    a named regression test rather than a comment and a hope.

**Do not delete this file.** It is the only thing standing between a convenience for
local development and an authentication bypass in production. CLAUDE.md lists it among
the footguns that must not be "fixed".
"""

from __future__ import annotations

import typing

import httpx
import pytest

from coach.api.auth import DEV_TOKEN_PREFIX, Authenticated
from coach.core.config import Env, Settings
from coach.core.errors import NotAuthenticated
from coach.main import create_app

#: Every `ENV` value that is not "local", derived from the type rather than written out,
#: so that adding a new environment to the union adds it to this test automatically.
NON_LOCAL_ENVS: list[str] = [e for e in typing.get_args(Env) if e != "local"]


def _deployed_settings(env: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A settings object for a deployed environment.

    Every field `ENV != local` demands has to be supplied, which is itself a small
    reminder of how much configuration the bypass is standing in for. The session's
    `FIRESTORE_EMULATOR_HOST` is hidden first: `Settings` refuses to combine it with a
    non-local `ENV`, which is a different guard from the one under test here.
    """
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    return Settings(
        env=env,  # type: ignore[arg-type]
        google_cloud_project="coach-test",
        artifact_bucket="coach-test-artifacts",
        upload_bucket="coach-test-uploads",
        tasks_queue="projects/coach-test/locations/us-central1/queues/autonomous-runs",
        tasks_target_url="https://coach.example/internal/runs",
        tasks_invoker_sa="coach-tasks-sa@coach-test.iam.gserviceaccount.com",
        allowed_scheduler_sa="coach-scheduler-sa@coach-test.iam.gserviceaccount.com",
        allowed_tasks_sa="coach-tasks-sa@coach-test.iam.gserviceaccount.com",
        oauth_client_id="1234.apps.googleusercontent.com",
        log_level="WARNING",
    )


async def test_dev_token_is_accepted_when_env_is_local(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me")
    assert response.status_code == 200
    assert response.json()["uid"] == "u_alice"


async def test_dev_token_uid_must_not_be_empty(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {DEV_TOKEN_PREFIX}"},
    ) as http_client:
        response = await http_client.get("/api/me")
    assert response.status_code == 401


@pytest.mark.parametrize("env", NON_LOCAL_ENVS)
async def test_dev_token_is_rejected_for_every_non_local_env(
    env: str, tmp_path, monkeypatch
) -> None:
    """A `dev:` token must be worth nothing outside ENV=local."""
    # A deployed app mounts `static/assets` unconditionally and fails at construction if
    # it is missing — the documented early failure that catches a wrong WORKDIR
    # (docs/07-infra-deploy.md#container). Give it one so this test fails for its own
    # reason and not that one.
    (tmp_path / "static" / "assets").mkdir(parents=True)
    (tmp_path / "static" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    settings = _deployed_settings(env, monkeypatch)
    app = create_app(settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": "Bearer dev:someuid"},
    ) as http_client:
        response = await http_client.get("/api/me")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("env", NON_LOCAL_ENVS)
async def test_dev_branch_is_not_reachable_outside_local(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stronger than the 401: assert the dev branch never *executes*.

    A 401 alone would still pass if the bypass ran and then failed for some unrelated
    reason. This replaces real token verification with a tripwire, so the only way to
    reach a 401 is via the verification path.
    """
    verification_attempted: list[str] = []

    def _tripwire(settings, token, *, check_revoked):
        verification_attempted.append(token)
        raise NotAuthenticated("verification path reached")

    monkeypatch.setattr("coach.api.auth.verify_id_token", _tripwire)

    settings = _deployed_settings(env, monkeypatch)
    request = _fake_request(settings, "Bearer dev:someuid")

    with pytest.raises(NotAuthenticated):
        await Authenticated()(request)

    # The dev branch would have returned a Principal without ever calling this.
    assert verification_attempted == ["dev:someuid"]


async def test_dev_branch_is_reachable_only_via_the_prefix(settings) -> None:
    """Under ENV=local, a token without the `dev:` prefix still goes to verification."""
    request = _fake_request(settings, "Bearer not-a-dev-token")
    with pytest.raises(Exception) as excinfo:
        await Authenticated()(request)
    # Reaching real verification without credentials is the expected failure here; the
    # point is that it was *reached*.
    assert not isinstance(excinfo.value, AssertionError)


@pytest.mark.parametrize(
    "header",
    ["", "Basic dev:someuid", "Bearer", "dev:someuid", "Bearer    "],
)
async def test_malformed_authorization_headers_are_rejected(settings, header: str) -> None:
    request = _fake_request(settings, header)
    with pytest.raises(NotAuthenticated):
        await Authenticated()(request)


def _fake_request(settings: Settings, authorization: str):
    """A minimal ASGI request carrying just an Authorization header and app state."""
    from starlette.datastructures import State
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/me",
        "headers": ([(b"authorization", authorization.encode())] if authorization else []),
        "app": type("App", (), {"state": State({"settings": settings})})(),
    }
    return Request(scope)
