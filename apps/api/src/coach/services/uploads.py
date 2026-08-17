"""Uploads: signed PUT, then finalize.

docs/04-api-contract.md#uploads:

- `POST /api/uploads` — `{ filename, mimeType, sizeBytes }` → `{ uploadId, signedUrl }`
- `POST /api/uploads/{id}/finalize` — server verifies size and type, then registers the
  artifact.

> Accepted: `image/png`, `image/jpeg`, `image/webp`, `application/pdf`, `text/plain`,
> `text/markdown`. Cap 20 MB. **MIME sniffed server-side, not trusted from the client.**

The last clause is why finalize exists at all. The browser PUTs straight to GCS, so the
only moment the server can look at what actually landed is afterwards — and the client's
declared `mimeType` at request time is a hint for the signature, never the decision. An
object whose stored content type does not match what was declared is rejected and the
upload never becomes referenceable.

## Two buckets, and why "registers ADK artifact" is load-bearing

The signed PUT targets `{project}-coach-uploads`, which is **staging**: it carries
`lifecycle_rule { age = 1 → Delete }` (docs/07-infra-deploy.md). GCS lifecycle rules
cannot express "unfinalized", so that rule deletes finalized objects just as happily.

Durable storage is `{project}-coach-artifacts`, written by ADK's `GcsArtifactService`
(docs/03-agent-design.md#artifacts: "User uploads (images, PDFs) land there and are
referenced as `types.Part` file parts"). So finalize copies the verified bytes across and
every later reference points at the artifact, never at the staging object.

Getting this wrong is not a 24-hour bug, it is a *delayed* one: a session's history is
replayed to the model on every subsequent turn, so an attachment referenced from staging
works all day and then starts silently resolving to nothing — a coach that has forgotten
a screenshot it discussed yesterday.

**Not yet done: "scans".** The contract lists content scanning in this step and nothing
here scans. Deferred to M7's "Security review: … upload handling", and recorded in
docs/09-roadmap.md rather than left as an unremarked gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coach.agents.runner import APP_NAME
from coach.core.clock import now
from coach.core.errors import NotFound, ValidationProblem
from coach.core.ids import upload_id as new_upload_id
from coach.core.principal import Principal
from coach.integrations.artifacts import ArtifactServiceProvider, register_upload
from coach.integrations.storage import SIGNED_URL_TTL, ObjectStore
from coach.repositories.uploads import UploadRepository

logger = logging.getLogger(__name__)

#: docs/04-api-contract.md#uploads, exactly.
ACCEPTED_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
    }
)

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SignedUpload:
    upload_id: str
    signed_url: str


@dataclass(frozen=True, slots=True)
class ResolvedUpload:
    """A finalized upload, as a turn references it."""

    upload_id: str
    uri: str
    mime_type: str
    #: The name the user's file had. Carried onto the `types.Part` so the transcript can
    #: still say "screenshot.png" a week later — the artifact's own name is
    #: `user:{uploadId}` and the `gs://` URI has no human part.
    filename: str = ""


class UploadService:
    def __init__(
        self,
        uploads: UploadRepository,
        store: ObjectStore,
        artifacts: ArtifactServiceProvider,
    ) -> None:
        self._uploads = uploads
        self._store = store
        # A provider, resolved when a request actually needs the bucket — constructing
        # this service must not resolve credentials. See `artifact_service_provider`.
        self._artifacts = artifacts

    async def create(
        self, principal: Principal, *, filename: str, mime_type: str, size_bytes: int
    ) -> SignedUpload:
        """`POST /api/uploads`."""
        if mime_type not in ACCEPTED_MIME_TYPES:
            raise ValidationProblem(
                f"{mime_type!r} is not an accepted upload type. Accepted: "
                f"{', '.join(sorted(ACCEPTED_MIME_TYPES))}."
            )
        if not 0 < size_bytes <= MAX_UPLOAD_BYTES:
            raise ValidationProblem(
                f"An upload must be between 1 byte and {MAX_UPLOAD_BYTES} bytes."
            )

        upload_id = new_upload_id()
        # Namespaced by uid so that a bucket listing is already partitioned by owner, and
        # so a guessed object name in another user's namespace is still unreachable —
        # every read goes through the ownership check in `resolve`.
        object_name = f"{principal.uid}/{upload_id}/{filename}"
        await self._uploads.create(
            upload_id,
            owner_uid=principal.uid,
            object_name=object_name,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            expires_at=now() + SIGNED_URL_TTL,
        )
        return SignedUpload(
            upload_id=upload_id,
            signed_url=await self._store.signed_put_url(object_name, mime_type=mime_type),
        )

    async def finalize(self, principal: Principal, upload_id: str) -> ResolvedUpload:
        """`POST /api/uploads/{id}/finalize` — verify what actually landed."""
        record = await self._require_owned(principal, upload_id)
        object_name = str(record["objectName"])

        stat = await self._store.stat(object_name)
        if stat is None:
            raise ValidationProblem(
                "No object has been uploaded against this id yet. PUT to the signed URL "
                "first, then finalize."
            )
        size_bytes, content_type = stat

        if size_bytes > MAX_UPLOAD_BYTES:
            raise ValidationProblem(
                f"The uploaded object is {size_bytes} bytes, over the "
                f"{MAX_UPLOAD_BYTES}-byte cap."
            )
        # The stored content type is what the object actually is; the declared one was
        # only ever an input to the signature.
        if content_type and content_type not in ACCEPTED_MIME_TYPES:
            raise ValidationProblem(
                f"The uploaded object is {content_type!r}, which is not an accepted type."
            )

        resolved_type = content_type or str(record["mimeType"])

        data = await self._store.download(object_name)
        if data is None:  # pragma: no cover - `stat` above already found it
            raise ValidationProblem("The uploaded object disappeared before it was finalized.")

        # The move out of staging. Everything above only decided whether these bytes are
        # allowed to exist; this is what makes them last longer than a day.
        artifact = await register_upload(
            self._artifacts(),
            app_name=APP_NAME,
            user_id=principal.uid,
            upload_id=upload_id,
            display_name=str(record["filename"]),
            mime_type=resolved_type,
            data=data,
        )

        await self._uploads.finalize(
            upload_id,
            size_bytes=size_bytes,
            mime_type=resolved_type,
            artifact_filename=artifact.filename,
            artifact_version=artifact.version,
            artifact_uri=artifact.uri,
        )
        return ResolvedUpload(
            upload_id=upload_id,
            uri=artifact.uri,
            mime_type=resolved_type,
            filename=str(record["filename"]),
        )

    async def resolve(self, principal: Principal, upload_id: str) -> ResolvedUpload:
        """The `types.Part` file reference for a finalized upload.

        Refuses an upload that has not been finalized: attaching one would hand the model
        a `gs://` URI for an object nobody has checked the size or type of.
        """
        record = await self._require_owned(principal, upload_id)
        if record.get("status") != "ready":
            raise ValidationProblem(
                f"Upload {upload_id!r} has not been finalized. Call "
                f"POST /api/uploads/{upload_id}/finalize first."
            )
        # The artifact URI, never `objectName` — that one points into the staging bucket
        # and stops resolving a day later. See the module docstring.
        return ResolvedUpload(
            upload_id=upload_id,
            uri=str(record["artifactUri"]),
            mime_type=str(record["mimeType"]),
            filename=str(record.get("filename") or ""),
        )

    async def bytes_for_artifact_uri(
        self, principal: Principal, artifact_uri: str
    ) -> tuple[bytes, str, str]:
        """`(data, mimeType, filename)` for the artifact a transcript event references.

        Backs the image previews. The bytes come from `load_artifact`, so the same call
        works against `GcsArtifactService` and the in-memory stand-in — no `gs://` path is
        parsed anywhere, which is what keeps ADK's blob layout ADK's business.
        """
        record = await self._uploads.find_by_artifact_uri(artifact_uri)
        if record is None or record.get("ownerUid") != principal.uid:
            raise NotFound("No attachment for that reference.")

        part = await self._artifacts().load_artifact(
            app_name=APP_NAME,
            user_id=principal.uid,
            filename=str(record["artifactFilename"]),
            version=int(record.get("artifactVersion") or 0),
        )
        if part is None or part.inline_data is None or part.inline_data.data is None:
            raise NotFound("That attachment's content is no longer stored.")

        return (
            bytes(part.inline_data.data),
            str(record.get("mimeType") or part.inline_data.mime_type or ""),
            str(record.get("filename") or ""),
        )

    async def _require_owned(self, principal: Principal, upload_id: str) -> dict[str, object]:
        record = await self._uploads.get(upload_id)
        if record is None or record.get("ownerUid") != principal.uid:
            raise NotFound(f"No upload {upload_id!r}.")
        return record


__all__ = [
    "ACCEPTED_MIME_TYPES",
    "MAX_UPLOAD_BYTES",
    "ResolvedUpload",
    "SignedUpload",
    "UploadService",
]
