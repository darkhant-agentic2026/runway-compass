"""`uploads/{uploadId}` — what a signed upload URL was issued for.

docs/04-api-contract.md gives a two-step upload: `POST /api/uploads` returns a V4
resumable signed URL, the browser PUTs to GCS directly, and `POST /api/uploads/{id}/finalize`
has the server verify size and type and register the ADK artifact. The server is out of
the data path in between, so *something* has to remember which object an id refers to and
who may reference it — that is this document.

The collection is not in docs/02-data-model.md's original map, and is added there with
this module for the same reason `idempotency/*` was at M1: the contract needs
cross-instance state that no existing collection holds. It is scoped by `ownerUid`, and
`UploadService` checks that on every read, so an upload id from one account cannot be
attached to another account's turn.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from coach.core.clock import now
from coach.repositories.firestore import Database

UPLOADS = "uploads"


class UploadRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _doc(self, upload_id: str) -> Any:
        return self._db.client.collection(UPLOADS).document(upload_id)

    async def create(
        self,
        upload_id: str,
        *,
        owner_uid: str,
        object_name: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        expires_at: datetime,
    ) -> None:
        await self._doc(upload_id).set(
            {
                "ownerUid": owner_uid,
                "objectName": object_name,
                "filename": filename,
                "mimeType": mime_type,
                "sizeBytes": size_bytes,
                "status": "pending",
                "createdAt": now(),
                # The bucket has a one-day lifecycle rule on unfinalized objects
                # (docs/07-infra-deploy.md); this mirrors it so a stale record cannot
                # outlive the object it points at.
                "expiresAt": expires_at,
            }
        )

    async def get(self, upload_id: str) -> dict[str, Any] | None:
        snapshot = await self._doc(upload_id).get()
        if not snapshot.exists:
            return None
        return snapshot.to_dict() or {}

    async def find_by_artifact_uri(self, artifact_uri: str) -> dict[str, Any] | None:
        """The upload behind a `gs://` reference in a transcript.

        A stored event carries only the artifact URI, so this is how a preview request
        gets back to the artifact's name and version without the caller parsing ADK's blob
        layout out of the URI.

        **One filter, deliberately.** `ownerUid` is checked by the service on the result
        rather than added here: a second `where` would make this a composite query needing
        an index that does not exist, and real Firestore answers that with
        `FAILED_PRECONDITION` while the emulator answers correctly
        (see `CLAUDE.md`, and `CoachSessionService.find_session_id_for_task`).
        """
        query = (
            self._db.client.collection(UPLOADS)
            .where(filter=FieldFilter("artifactUri", "==", artifact_uri))
            .limit(2)
        )
        async for document in query.stream():
            return document.to_dict() or {}
        return None

    async def finalize(
        self,
        upload_id: str,
        *,
        size_bytes: int,
        mime_type: str,
        artifact_filename: str,
        artifact_version: int,
        artifact_uri: str,
    ) -> None:
        """Record what was verified, and where the durable copy went.

        `artifactUri` is what every later reference uses; `objectName` keeps pointing at
        the staging object, which the bucket's one-day lifecycle rule will collect. The
        two are deliberately both kept — the first is the answer, the second is the
        provenance.
        """
        await self._doc(upload_id).update(
            {
                "status": "ready",
                "sizeBytes": size_bytes,
                "mimeType": mime_type,
                "artifactFilename": artifact_filename,
                "artifactVersion": artifact_version,
                "artifactUri": artifact_uri,
                "finalizedAt": now(),
                # The record's own expiry, not the object's: the staging object is still
                # collected on schedule, but the upload it produced is now durable.
                "expiresAt": None,
            }
        )


__all__ = ["UPLOADS", "UploadRepository"]
