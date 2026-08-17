"""Building the app resolves no credentials.

`coach/repositories/firestore.py` states the invariant on `Database`: a Firestore client
is built on first use, never in a constructor, because constructing one resolves
Application Default Credentials — and `coach.main` creates the app at module scope, since
`uvicorn coach.main:app` needs it to exist. An eager client therefore turns
`import coach.main` into a credentials check.

M2 broke it by handing `get_client(settings)` to `CoachSessionService`, whose constructor
takes a client rather than making one. **Every local test still passed**, because
`./scripts/dev.sh test api` exports `FIRESTORE_EMULATOR_HOST` before pytest starts and the
client goes anonymous when it is set. CI has no such variable at *collection* time — the
emulator there is started by a session fixture, which runs after collection has already
imported the app — so four modules failed to import with `DefaultCredentialsError`.

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


def test_the_container_can_be_built_without_credentials(no_credentials: None) -> None:
    """The whole dependency graph, including the ADK session service."""
    Container(Settings(env="local", google_cloud_project="demo-coach-test"))


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
