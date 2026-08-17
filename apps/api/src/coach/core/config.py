"""Application settings.

The `.env` contract is specified in docs/07-infra-deploy.md#local-development. It is
validated here at startup, fail-fast: a misconfigured revision should refuse to boot
rather than serve requests against the wrong database.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Env = Literal["local", "dev", "prod"]
#: `stub` is the deterministic model the end-to-end harness runs against
#: (docs/08-testing.md). It is refused for every non-`local` `ENV` below, on the same
#: footing as the `Bearer dev:<uid>` auth path: deliberate test-only code, guarded by one
#: check, with a named regression test rather than a comment and a hope.
ModelBackend = Literal["vertex", "gemini_api", "stub"]

#: Fields that only a deployed environment can supply. Required when ENV != "local";
#: absent locally, where the corresponding surfaces are stubbed or unused.
_DEPLOYED_REQUIRED = (
    "artifact_bucket",
    "upload_bucket",
    "tasks_queue",
    "tasks_target_url",
    "tasks_invoker_sa",
    "allowed_scheduler_sa",
    "allowed_tasks_sa",
    "oauth_client_id",
)


class Settings(BaseSettings):
    """Validated process configuration.

    Instantiated once via :func:`get_settings`. Tests build their own instances
    directly, which is why the constructor takes no required positional arguments.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        # `MODEL_NAME` and `MODEL_BACKEND` would otherwise collide with pydantic's
        # protected `model_` namespace.
        protected_namespaces=(),
    )

    env: Env = "local"
    google_cloud_project: str = "coach-local"

    # --- Model access ----------------------------------------------------------------
    model_backend: ModelBackend = "vertex"
    model_name: str = "gemini-3.7-flash"
    vertex_location: str = "us-central1"
    gemini_api_key: str | None = None

    # --- Firestore -------------------------------------------------------------------
    firestore_database: str = "(default)"
    #: Local only. The google-cloud-firestore client reads this variable itself; we mirror
    #: it into Settings so the non-local guard below can see it.
    firestore_emulator_host: str | None = None
    #: Pinned explicitly rather than left to ADK's own default. docs/02-data-model.md
    adk_firestore_root_collection: str = "adk-session"

    # --- Storage ---------------------------------------------------------------------
    artifact_bucket: str | None = None
    upload_bucket: str | None = None

    # --- Cloud Tasks -----------------------------------------------------------------
    tasks_queue: str | None = None
    tasks_target_url: str | None = None
    #: OIDC identity minted onto each enqueued task.
    tasks_invoker_sa: str | None = None

    # --- External APIs ---------------------------------------------------------------
    youtube_api_key: str | None = None

    # --- Inbound OIDC allow-lists ----------------------------------------------------
    # Two *different* service accounts call /internal/*. Collapsing them into one variable
    # would let Cloud Scheduler invoke the executor, or the reverse. docs/07-infra-deploy.md
    allowed_scheduler_sa: str | None = None
    allowed_tasks_sa: str | None = None

    # --- Identity Platform -----------------------------------------------------------
    #: Google provider client id; also the expected audience of inbound ID tokens.
    oauth_client_id: str | None = None

    # --- Runtime ---------------------------------------------------------------------
    log_level: str = "INFO"
    #: Caps concurrent agent runs per instance so background work cannot starve
    #: interactive turns. docs/07-infra-deploy.md
    max_concurrent_agent_runs: int = Field(default=8, ge=1)

    @property
    def is_local(self) -> bool:
        return self.env == "local"

    @model_validator(mode="after")
    def _check_emulator_is_local_only(self) -> Settings:
        """A deployed revision pointing at an emulator host reaches nothing at all.

        This is the guard docs/07-infra-deploy.md calls for, and it fails at import time
        rather than on the first Firestore read.
        """
        if self.firestore_emulator_host and self.env != "local":
            raise ValueError(
                f"FIRESTORE_EMULATOR_HOST is set ({self.firestore_emulator_host!r}) but "
                f"ENV={self.env!r}. The emulator is local-only; a deployed revision "
                "pointing at it would silently read and write nothing."
            )
        return self

    @model_validator(mode="after")
    def _check_gemini_api_key(self) -> Settings:
        if self.model_backend == "gemini_api" and not self.gemini_api_key:
            raise ValueError(
                "MODEL_BACKEND=gemini_api requires GEMINI_API_KEY. Production uses "
                "MODEL_BACKEND=vertex, which authenticates as the service account and "
                "needs no key."
            )
        return self

    @model_validator(mode="after")
    def _check_stub_model_is_local_only(self) -> Settings:
        """A deployed revision must never serve canned answers.

        Failing to start is much better than the alternative, which would look exactly
        like the product working — the coach would reply, the board would update, and
        nobody would find out until someone read a transcript.
        """
        if self.model_backend == "stub" and self.env != "local":
            raise ValueError(
                f"MODEL_BACKEND=stub is the deterministic end-to-end stub and is only "
                f"valid for ENV=local; got ENV={self.env!r}."
            )
        return self

    @model_validator(mode="after")
    def _check_deployed_fields(self) -> Settings:
        if self.env == "local":
            return self
        missing = sorted(f.upper() for f in _DEPLOYED_REQUIRED if not getattr(self, f))
        if missing:
            raise ValueError(
                f"ENV={self.env!r} requires these settings, which are unset: "
                f"{', '.join(missing)}"
            )
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once.

    Cached so that the `.env` files are parsed a single time and so that FastAPI's
    dependency graph shares one instance. Tests that need a different configuration
    override the FastAPI dependency rather than clearing this cache.
    """
    return Settings()
