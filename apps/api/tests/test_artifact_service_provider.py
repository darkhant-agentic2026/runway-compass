"""What the container hands ADK has to *be* an artifact service, not act like one.

Closing M2 deferred `GcsArtifactService` behind `LazyProxy`, because its constructor
resolves Application Default Credentials and assembling the app must not
(`test_import_without_credentials.py`). The whole local suite passed. The deployed
revision then failed the first turn of every conversation:

    1 validation error for InvocationContext
    artifact_service Input should be an instance of BaseArtifactService

`Runner` puts the artifact service on an `InvocationContext`, which is a pydantic model,
and pydantic checks the type rather than the attributes. Nothing local reached that branch:
without `ARTIFACT_BUCKET` the in-memory service is used, and it is a real instance.

The second symptom was silent, and is the reason this file also asserts a URI. The proxy
refuses underscore attributes to stay recursion-safe, so `artifact_part_uri`'s
`getattr(service, "_get_blob_name", None)` came back `None` and every finalized upload was
recorded with an `artifact://` URI instead of a `gs://` one — a reference the model cannot
dereference, on an upload that otherwise looked fine end to end.

Both cases are pinned here against a **deployed** configuration with a fake
`storage.Client`, because a deployed `ENV` is the only one that builds the GCS-backed
service and because faking the client is what lets it be built at all without credentials.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.sessions.session import Session

from coach.agents.prompt import PromptBuilder
from coach.agents.research_tools import ResearchTools
from coach.agents.runner import RunnerFactory
from coach.agents.tools import DomainTools
from coach.core.app import APP_NAME
from coach.core.config import Settings
from coach.integrations.artifacts import artifact_part_uri, artifact_service_provider
from coach.ws.hub import BoardUpdateHub
from test_import_without_credentials import DEPLOYED


class FakeStorageClient:
    """Enough of `google.cloud.storage.Client` for `GcsArtifactService.__init__`.

    It resolves no credentials, which is the point: these tests are about the *type* of
    the service, and they must run on a machine that has no ADC and in CI.
    """

    def __init__(self, **_kwargs: Any) -> None:
        self.buckets: list[str] = []

    def bucket(self, name: str) -> object:
        self.buckets.append(name)
        return object()


@pytest.fixture
def deployed_artifacts(monkeypatch: pytest.MonkeyPatch):
    """A provider for a deployed `ENV`, with the storage client faked out."""
    from google.cloud import storage

    monkeypatch.setattr(storage, "Client", FakeStorageClient)
    # `Settings` refuses a deployed `ENV` while the emulator host is set, and
    # `dev.sh test api` exports it. Nothing here touches Firestore.
    monkeypatch.delenv("FIRESTORE_EMULATOR_HOST", raising=False)
    settings = Settings(env="dev", google_cloud_project="coach-dev", **DEPLOYED)
    return artifact_service_provider(settings)


def test_the_deployed_artifact_service_is_an_instance_adk_will_accept(
    deployed_artifacts,
) -> None:
    """The direct check, stated the way pydantic states it."""
    assert isinstance(deployed_artifacts(), BaseArtifactService)


async def test_a_runner_can_open_an_invocation_context(deployed_artifacts) -> None:
    """The failure as production met it.

    `isinstance` above is the *claim*; this is the path that made it matter. The runner is
    built the way the container builds it and asked for an invocation context, which is
    where `Runner` validates the services it was given — note that constructing the
    `Runner` itself does not, which is why the M2 suite could hold a broken one and pass.

    `_new_invocation_context` is private, and calling it is deliberate on the same terms as
    `_get_blob_name` in `integrations/artifacts.py`: an upstream rename fails here, loudly
    and by name, instead of at a deployed turn. docs/03-agent-design.md#bumping-the-adk-version
    """
    from streaming_doubles import ScriptedModel

    factory = RunnerFactory(
        Settings(env="dev", google_cloud_project="coach-dev", **DEPLOYED),
        InMemorySessionService(),
        deployed_artifacts,
        # Neither the tools nor the prompt builder is exercised here: this test opens
        # an invocation context and looks at one field on it. They are built over no
        # services rather than mocked, because a mock would imply they were part of what
        # is under test.
        tools=DomainTools(cast(Any, None), cast(Any, None), BoardUpdateHub()),
        research_tools=ResearchTools(cast(Any, None), cast(Any, None), BoardUpdateHub()),
        prompt=PromptBuilder(*(cast(Any, None),) * 4),
    )
    # A scripted model so that no model client is built either; the artifact service is
    # what is under test.
    factory.set_model(ScriptedModel(chunks=["hi"], invocations=[]))
    runner = factory.runner()

    context = runner._new_invocation_context(
        Session(id="s_1", app_name=APP_NAME, user_id="u_alice")
    )

    assert context.artifact_service is deployed_artifacts()


def test_a_deployed_upload_gets_a_gs_uri(deployed_artifacts) -> None:
    """The quiet half: the service must still expose the blob layout it is read for.

    `artifact_part_uri` falls back to `artifact://` for the in-memory service, which has no
    bucket. Anything that hides `bucket_name` or `_get_blob_name` — a proxy did — takes
    that fallback in production, where the URI is handed to the model.
    """
    uri = artifact_part_uri(
        deployed_artifacts(),
        app_name=APP_NAME,
        user_id="u_alice",
        filename="user:up_1",
        version=0,
    )

    assert uri == f"gs://{DEPLOYED['artifact_bucket']}/coach/u_alice/user/user:up_1/0"


def test_the_process_shares_one_service(deployed_artifacts) -> None:
    """`UploadService` writes the artifact and the agent reads it back through `Runner`.

    Two instances would be two `storage.Client`s against one bucket, and two places to
    disagree about which bucket that is — so the provider memoises rather than building
    per call.
    """
    assert deployed_artifacts() is deployed_artifacts()
