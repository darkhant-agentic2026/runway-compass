"""Artifact storage for user uploads.

docs/03-agent-design.md#artifacts: `GcsArtifactService` from ADK, pointed at
`gs://{project}-coach-artifacts`. User uploads (images, PDFs) land there and are
referenced as `types.Part` file parts. "No custom work" — so this module is construction
and a local stand-in, nothing more.

The local stand-in is `InMemoryArtifactService`, which ADK ships. docs/07-infra-deploy.md
records that storage is one of the two local dependencies that are *not* emulated: real
uploads need a real V4 signer, so local development points at a real `coach-dev` bucket
with impersonated credentials. Tests take neither path — they "fake the artifact service
outright and touch neither GCS nor a signer" (docs/08-testing.md), which is exactly what
the in-memory service is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import cast

from google.adk.artifacts.artifact_util import get_artifact_uri
from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService
from google.genai import types

from coach.core.config import Settings
from coach.core.lazy import LazyProxy

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    """A user upload that now lives in the artifacts bucket."""

    filename: str
    version: int
    #: What a `types.Part` in a turn points at. See `artifact_part_uri`.
    uri: str


def upload_artifact_filename(upload_id: str) -> str:
    """The artifact name for an upload.

    **`user:`-scoped, deliberately.** ADK scopes an artifact either to a session or to a
    user, and `POST /api/uploads` does not know a session — the contract's request body is
    `{ filename, mimeType, sizeBytes }` and an upload may be attached to any of the
    owner's sessions. User scope is the honest match, and it is also the only one whose
    blob path has no session segment to invent.

    The upload id rather than the human filename: it is a ULID, so two uploads called
    `screenshot.png` are two artifacts rather than two *versions* of one, and the display
    name is already on `uploads/{uploadId}`.
    """
    return f"user:{upload_id}"


def artifact_part_uri(
    service: BaseArtifactService, *, app_name: str, user_id: str, filename: str, version: int
) -> str:
    """The URI a `types.Part` should carry for a saved artifact.

    docs/00-overview.md: "images and PDFs are passed as `types.Part` **file references
    backed by GCS**". So this has to be a `gs://` URI — an `artifact://` reference is
    understood by ADK's artifact services only, not by the model flow, and Gemini would
    receive it verbatim.

    Constructing that URI needs the blob layout, which is `GcsArtifactService`'s and not
    ours. It is read back out of the service's own `_get_blob_name` rather than restated
    here, and the choice between two kinds of coupling is deliberate:

    - Duplicating the format means an upstream change to it produces a **wrong path**,
      silently, and the symptom is a model that cannot see an attachment.
    - Calling the private method means an upstream *rename* is an `AttributeError` on the
      first upload — loud, immediate, and caught by this module's tests — while an
      upstream change to the format is simply picked up.

    Loud beats silent here, so: the private method. This is the surface the "
    `GcsArtifactService(bucket_name, **kwargs)` construction, `types.Part` file
    references" row of docs/03-agent-design.md#bumping-the-adk-version already covers.

    The in-memory service has no bucket and no blob names. It only ever runs where
    nothing dereferences the URI — local development without `ARTIFACT_BUCKET`, and the
    tests — so it falls back to the canonical `artifact://` form, which at least names
    the thing correctly if it ever shows up in a log.
    """
    bucket = getattr(service, "bucket_name", None)
    blob_name = getattr(service, "_get_blob_name", None)
    if bucket is None or blob_name is None:
        return get_artifact_uri(app_name, user_id, filename, version)
    return f"gs://{bucket}/{blob_name(app_name, user_id, filename, version, None)}"


async def register_upload(
    service: BaseArtifactService,
    *,
    app_name: str,
    user_id: str,
    upload_id: str,
    display_name: str,
    mime_type: str,
    data: bytes,
) -> RegisteredArtifact:
    """Move a verified upload into the artifacts bucket. The "registers ADK artifact"
    half of `POST /api/uploads/{id}/finalize` (docs/04-api-contract.md#uploads).

    This is not bookkeeping. The staging bucket deletes every object at one day of age,
    so an attachment referenced straight from it survives the conversation that created
    it and then disappears — and because a session's history is replayed to the model on
    every subsequent turn, the damage shows up long after the upload, as a coach that has
    forgotten a screenshot it discussed yesterday.
    """
    filename = upload_artifact_filename(upload_id)
    version = await service.save_artifact(
        app_name=app_name,
        user_id=user_id,
        filename=filename,
        artifact=types.Part.from_bytes(data=data, mime_type=mime_type),
        custom_metadata={"displayName": display_name, "uploadId": upload_id},
    )
    return RegisteredArtifact(
        filename=filename,
        version=version,
        uri=artifact_part_uri(
            service, app_name=app_name, user_id=user_id, filename=filename, version=version
        ),
    )


def build_artifact_service(settings: Settings) -> BaseArtifactService:
    """The artifact service for this environment.

    A local run without `ARTIFACT_BUCKET` gets the in-memory service; a deployed run always
    has one, because `Settings` refuses to start without it.

    The GCS-backed one is wrapped in a `LazyProxy`, because `GcsArtifactService` builds a
    `storage.Client` in its constructor and that resolves Application Default Credentials.
    Assembling the container must not do that — see `coach.core.lazy`.
    """
    if not settings.artifact_bucket:
        if not settings.is_local:  # pragma: no cover - Settings rejects this earlier
            raise ValueError("ARTIFACT_BUCKET is required outside ENV=local.")
        logger.info("no ARTIFACT_BUCKET set; using the in-memory artifact service")
        return InMemoryArtifactService()

    bucket = settings.artifact_bucket

    def _build() -> BaseArtifactService:
        from google.adk.artifacts.gcs_artifact_service import GcsArtifactService

        return GcsArtifactService(bucket_name=bucket)

    return cast("BaseArtifactService", LazyProxy(_build))


__all__ = [
    "RegisteredArtifact",
    "artifact_part_uri",
    "build_artifact_service",
    "register_upload",
    "upload_artifact_filename",
]
