"""Building the app resolves no credentials.

`coach/repositories/firestore.py` states the invariant on `Database`: a Firestore client
is built on first use, never in a constructor, because constructing one resolves
Application Default Credentials — and `coach.main` creates the app at module scope, since
`uvicorn coach.main:app` needs it to exist. An eager client therefore turns
`import coach.main` into a credentials check.

M2 broke it twice. First by handing `get_client(settings)` to `CoachSessionService`, whose
constructor takes a client rather than making one; then — after that was fixed — by leaving
`GcsArtifactService` and `GcsObjectStore` eager, which resolves credentials for any
*deployed* `ENV`, where a bucket is configured.

**Every local test passed both times.** `./scripts/dev.sh test api` exports
`FIRESTORE_EMULATOR_HOST` before pytest starts, which makes the Firestore client anonymous,
and this machine happens to have ADC, which satisfies the storage clients. CI has neither.

So the cases below cover `local` *and* deployed settings: the second failure was invisible
to the first version of this file, which only built a local container.

These tests are written against a **forced** absence of credentials rather than a real
one, so they fail on a developer's laptop too. A test that only fails on a machine without
ADC is a test that only fails in CI, which is the situation it exists to prevent.
"""

from __future__ import annotations

import google.auth
import pytest
from google.auth.exceptions import DefaultCredentialsError

from coach.api.deps import Container
from coach.core.config import Settings


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither an emulator nor ADC, however the host is actually configured."""

    def _refuse(*_args: object, **_kwargs: object) -> object:
        raise DefaultCredentialsError("no credentials, on purpose")

    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    # Patched on the module, because `google.cloud.client` calls `google.auth.default(...)`
    # by attribute at call time rather than importing the name.
    monkeypatch.setattr(google.auth, "default", _refuse)
    # The client is cached per (project, database); a cached one from another test would
    # hide the very construction under test.
    from coach.repositories import firestore

    firestore._client_for.cache_clear()


#: The configuration `Settings` demands before it will validate a deployed `ENV`.
DEPLOYED = {
    "artifact_bucket": "coach-dev-coach-artifacts",
    "upload_bucket": "coach-dev-coach-uploads",
    "tasks_queue": "projects/p/locations/l/queues/q",
    "tasks_target_url": "https://coach-dev.example/internal/runs",
    "tasks_invoker_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_scheduler_sa": "coach-scheduler-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_tasks_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "oauth_client_id": "1234.apps.googleusercontent.com",
}


def test_the_container_can_be_built_without_credentials(no_credentials: None) -> None:
    """The whole dependency graph, including the ADK session service."""
    Container(Settings(env="local", google_cloud_project="demo-coach-test"))


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_a_deployed_container_can_be_built_without_credentials(
    no_credentials: None, env: str
) -> None:
    """The case the first fix missed.

    A deployed `ENV` has both buckets set, so `build_artifact_service` and
    `build_object_store` reach their GCS-backed branches — and those constructors resolve
    credentials unless the client is deferred.
    """
    Container(Settings(env=env, google_cloud_project="coach-dev", **DEPLOYED))


def test_the_app_can_be_created_without_credentials(
    no_credentials: None, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `import coach.main` does, and what CI does at collection time."""
    monkeypatch.chdir(tmp_path)
    from coach.main import create_app

    create_app(Settings(env="local", google_cloud_project="demo-coach-test"))


def test_touching_firestore_is_what_finally_needs_them(no_credentials: None) -> None:
    """The other half: laziness must defer the credential check, not skip it.

    Without this a proxy that silently swallowed the failure would pass the two tests
    above and turn a misconfigured deployment into a mystery instead of a `/readyz`
    failure.
    """
    container = Container(Settings(env="local", google_cloud_project="demo-coach-test"))

    with pytest.raises(DefaultCredentialsError):
        _ = container.db.client

    with pytest.raises(DefaultCredentialsError):
        container.session_service.client.collection("adk-session")


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_the_artifact_service_is_built_when_it_is_first_asked_for(
    no_credentials: None, env: str
) -> None:
    """Same half, for the deferral that is a provider rather than a proxy.

    `Container.artifacts` is a callable, so "deferred" here means *nothing calls it while
    assembling the app* — a property that no type can enforce and that this asserts
    directly: the deployed container above was built with no credentials, and asking its
    provider for the service is what finally needs them. See
    `integrations/artifacts.artifact_service_provider` for why it is not a `LazyProxy`,
    and `test_artifact_service_provider.py` for what the proxy broke.
    """
    container = Container(Settings(env=env, google_cloud_project="coach-dev", **DEPLOYED))

    with pytest.raises(DefaultCredentialsError):
        container.artifacts()
