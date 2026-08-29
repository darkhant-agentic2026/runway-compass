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
    # docs/04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas: the
    # production default. `docker-compose.e2e.yml` overrides it via `NEW_USER_RATE_LIMIT`
    # for its own reason (a fresh uid per test, not a real signup); nothing else should.
    assert settings.new_user_rate_limit == 4
    assert settings.new_user_rate_limit_window_minutes == 30


def test_new_user_rate_limit_is_overridable_by_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the exact mechanism `docker-compose.e2e.yml`'s `NEW_USER_RATE_LIMIT` relies
    on, so a rename here fails a test rather than silently breaking every e2e run."""
    monkeypatch.setenv("NEW_USER_RATE_LIMIT", "100000")
    settings = Settings(env="local")
    assert settings.new_user_rate_limit == 100000


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
    from coach.integrations.model import TokenRateLimiter, build_model
    from coach.integrations.stub_model import StubModel

    settings = Settings(env="local", model_backend="stub")
    assert isinstance(build_model(settings, TokenRateLimiter()), StubModel)


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


# --- unseeded secrets -------------------------------------------------------------------
# `terraform apply` creates each Secret Manager secret with a placeholder first version and
# leaves the real value to RUNBOOK §4, a human step. Nothing fails at boot when it is
# skipped, which is how M4 shipped a deployed revision whose research reports silently
# contained no videos: the placeholder is a non-empty string, so the YouTube client thought
# it had a key, sent it to Google, and reported the resulting 400 as "the YouTube API did
# not answer".


def test_the_placeholder_matches_the_terraform_that_writes_it() -> None:
    """The constant is restated across an HCL/Python boundary, so it is pinned by a test.

    Same reasoning as the theme's `localStorage` key and ADK's
    `adk_request_confirmation`: two files have to agree about a literal and neither can
    import the other. A drift here is silent in exactly the direction that matters —
    Terraform starts writing a *different* placeholder, Python stops recognising it, and
    the failure goes back to being a 400 from Google.
    """
    from pathlib import Path

    from coach.core.config import SECRET_PLACEHOLDER

    root = Path(__file__).resolve().parents[3] / "infra" / "terraform" / "envs"
    written = {
        env: (root / env / "main.tf").read_text(encoding="utf-8") for env in ("dev", "prod")
    }
    for env, body in written.items():
        assert f'secret_placeholder = "{SECRET_PLACEHOLDER}"' in body, env


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_a_placeholder_youtube_key_reads_as_absent(env: str) -> None:
    from coach.core.config import SECRET_PLACEHOLDER

    settings = Settings(env=env, youtube_api_key=SECRET_PLACEHOLDER, **DEPLOYED)

    assert settings.youtube_api_key is None
    assert settings.placeholder_secrets == ["youtube_api_key"]


def test_a_real_youtube_key_is_left_alone() -> None:
    settings = Settings(env="dev", youtube_api_key="AIza-real-looking-key", **DEPLOYED)

    assert settings.youtube_api_key == "AIza-real-looking-key"
    assert settings.placeholder_secrets == []


def test_a_missing_youtube_key_does_not_stop_a_deployment_booting() -> None:
    """Deliberately not fatal.

    A project can run with `allowVideos: false`, and refusing to start would take the whole
    service down over a feature it can do without. The compensating control is that the
    absence is *loud* — `main.py` logs it at startup and the tool logs every refusal — not
    that it is fatal.
    """
    settings = Settings(env="dev", **DEPLOYED)

    assert settings.youtube_api_key is None
    assert settings.placeholder_secrets == []


def test_a_placeholder_gemini_key_fails_the_backend_that_needs_one() -> None:
    """Here the loud treatment is free: the existing guard refuses to start, and after the
    placeholder is nulled it sees a backend with no key at all."""
    from coach.core.config import SECRET_PLACEHOLDER

    with pytest.raises(ValidationError, match="GEMINI_API_KEY"):
        Settings(env="local", model_backend="gemini_api", gemini_api_key=SECRET_PLACEHOLDER)
