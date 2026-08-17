"""`Settings` validation — the fail-fast `.env` contract from docs/07-infra-deploy.md."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coach.core.config import Settings


@pytest.fixture(autouse=True)
def _no_ambient_emulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the test session's emulator from `Settings`.

    The session fixture exports `FIRESTORE_EMULATOR_HOST` for the Firestore client, and
    pydantic-settings reads the environment — so without this every "deployed" case here
    would trip the local-only guard for the wrong reason.
    """
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)


DEPLOYED = {
    "google_cloud_project": "coach-dev",
    "artifact_bucket": "coach-dev-artifacts",
    "upload_bucket": "coach-dev-uploads",
    "tasks_queue": "projects/coach-dev/locations/us-central1/queues/autonomous-runs",
    "tasks_target_url": "https://coach-dev.example/internal/runs",
    "tasks_invoker_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_scheduler_sa": "coach-scheduler-sa@coach-dev.iam.gserviceaccount.com",
    "allowed_tasks_sa": "coach-tasks-sa@coach-dev.iam.gserviceaccount.com",
    "oauth_client_id": "1234.apps.googleusercontent.com",
}


def test_local_defaults_are_usable_without_configuration() -> None:
    settings = Settings(env="local")
    assert settings.is_local
    assert settings.model_name == "gemini-3.7-flash"
    assert settings.adk_firestore_root_collection == "adk-session"
    assert settings.firestore_database == "(default)"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_emulator_host_is_rejected_outside_local(env: str) -> None:
    """The guard docs/07-infra-deploy.md asks for.

    A deployed revision pointing at an emulator host reads and writes nothing at all,
    silently. This turns that into a refusal to boot.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(env=env, firestore_emulator_host="localhost:8081", **DEPLOYED)
    assert "FIRESTORE_EMULATOR_HOST" in str(excinfo.value)


def test_emulator_host_is_allowed_when_local() -> None:
    settings = Settings(env="local", firestore_emulator_host="localhost:8081")
    assert settings.firestore_emulator_host == "localhost:8081"


def test_gemini_backend_requires_an_api_key() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(env="local", model_backend="gemini_api")
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_gemini_backend_with_a_key_validates() -> None:
    settings = Settings(env="local", model_backend="gemini_api", gemini_api_key="k")
    assert settings.model_backend == "gemini_api"


def test_vertex_backend_needs_no_key() -> None:
    assert Settings(env="local", model_backend="vertex").gemini_api_key is None


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_the_stub_model_backend_is_refused_outside_local(env: str) -> None:
    """The end-to-end stub must never reach a deployed environment.

    This is deliberate test-only code on the same footing as the `Bearer dev:<uid>` auth
    path (docs/04-api-contract.md#authentication), and it gets a named regression test
    for the same reason: its failure mode is *silent success*. A deployed revision
    serving canned answers would reply, update the board, and look entirely healthy —
    the only symptom would be someone eventually reading a transcript.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(env=env, model_backend="stub", **DEPLOYED)
    assert "stub" in str(excinfo.value)


def test_the_stub_model_backend_is_allowed_when_local() -> None:
    assert Settings(env="local", model_backend="stub").model_backend == "stub"


def test_the_stub_model_is_what_the_stub_backend_builds() -> None:
    """Pins the wiring, not just the setting: the guard above is worthless if
    `build_model` ignored the backend and returned Gemini anyway."""
    from coach.integrations.model import build_model
    from coach.integrations.stub_model import StubModel

    assert isinstance(build_model(Settings(env="local", model_backend="stub")), StubModel)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_deployed_environments_require_their_configuration(env: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(env=env, google_cloud_project="coach-dev")
    message = str(excinfo.value)
    for expected in ("ARTIFACT_BUCKET", "TASKS_QUEUE", "OAUTH_CLIENT_ID"):
        assert expected in message


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_fully_configured_deployed_settings_validate(env: str) -> None:
    settings = Settings(env=env, **DEPLOYED)
    assert not settings.is_local
    assert settings.firestore_emulator_host is None


def test_the_two_internal_caller_service_accounts_stay_separate() -> None:
    """docs/07-infra-deploy.md: collapsing these into one variable would let Cloud
    Scheduler invoke the executor, or the reverse."""
    settings = Settings(env="dev", **DEPLOYED)
    assert settings.allowed_scheduler_sa != settings.allowed_tasks_sa


def test_an_unknown_env_value_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(env="staging")  # type: ignore[arg-type]
