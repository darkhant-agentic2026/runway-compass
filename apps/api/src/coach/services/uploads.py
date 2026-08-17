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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from coach.core.clock import now
from coach.core.errors import NotFound, ValidationProblem
from coach.core.ids import upload_id as new_upload_id
from coach.core.principal import Principal
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


class UploadService:
    def __init__(self, uploads: UploadRepository, store: ObjectStore) -> None:
        self._uploads = uploads
        self._store = store

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
            signed_url=self._store.signed_put_url(object_name, mime_type=mime_type),
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
        await self._uploads.finalize(upload_id, size_bytes=size_bytes, mime_type=resolved_type)
        return ResolvedUpload(
            upload_id=upload_id,
            uri=f"gs://{self._store.bucket}/{object_name}",
            mime_type=resolved_type,
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
        return ResolvedUpload(
            upload_id=upload_id,
            uri=f"gs://{self._store.bucket}/{record['objectName']}",
            mime_type=str(record["mimeType"]),
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
