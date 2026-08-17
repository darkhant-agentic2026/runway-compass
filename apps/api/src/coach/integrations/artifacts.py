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

from google.adk.artifacts.base_artifact_service import BaseArtifactService
from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService

from coach.core.config import Settings

logger = logging.getLogger(__name__)


def build_artifact_service(settings: Settings) -> BaseArtifactService:
    """The artifact service for this environment.

    Constructing `GcsArtifactService` builds a `storage.Client` eagerly, which resolves
    Application Default Credentials — so it is only reached when a bucket is actually
    configured. A local run without `ARTIFACT_BUCKET` gets the in-memory service and
    works; a deployed run always has one, because `Settings` refuses to start without it.
    """
    if not settings.artifact_bucket:
        if not settings.is_local:  # pragma: no cover - Settings rejects this earlier
            raise ValueError("ARTIFACT_BUCKET is required outside ENV=local.")
        logger.info("no ARTIFACT_BUCKET set; using the in-memory artifact service")
        return InMemoryArtifactService()

    from google.adk.artifacts.gcs_artifact_service import GcsArtifactService

    return GcsArtifactService(bucket_name=settings.artifact_bucket)


__all__ = ["build_artifact_service"]
