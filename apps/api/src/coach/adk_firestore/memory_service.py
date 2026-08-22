"""`CoachMemoryService` — ADK's shipped `FirestoreMemoryService` scoped to user memories.

docs/03-agent-design.md#coachmemoryservicefirestorememoryservice:
> The shipped `FirestoreMemoryService` already implements v1 exactly as this project wants it:
> keyword extraction with a stop-word list, storage with a `keywords[]` array, and
> `search_memory` as an `array_contains` fan-out over query terms. `add_session_to_memory` and
> `search_memory` are used as-is.
>
> The subclass adds only per-user collection placement (`users/{uid}/memories/{memoryId}`)
> and the `sourceSessionId` / `projectId` fields the UI attributes memories by.

Derived from `google-adk==2.7.0`,
`google/adk/integrations/firestore/firestore_memory_service.py`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, override

from google.adk.integrations.firestore.firestore_memory_service import (
    FirestoreMemoryService,
)
from google.adk.memory import _utils
from google.adk.memory.memory_entry import MemoryEntry
from google.cloud.firestore_v1.base_query import FieldFilter
from google.genai import types

if TYPE_CHECKING:
    from google.adk.events.event import Event
    from google.adk.sessions.session import Session
    from google.cloud import firestore

logger = logging.getLogger(__name__)

MEMORIES_COLLECTION = "memories"
USERS_COLLECTION = "users"


class CoachMemoryService(FirestoreMemoryService):
    """Subclass of `FirestoreMemoryService` placing memories under `users/{uid}/memories/*`."""

    def __init__(
        self,
        client: firestore.AsyncClient | None = None,
        stop_words: set[str] | None = None,
    ) -> None:
        super().__init__(client=client, stop_words=stop_words)

    def _user_memories_ref(self, user_id: str) -> Any:
        return (
            self.client.collection(USERS_COLLECTION)
            .document(user_id)
            .collection(MEMORIES_COLLECTION)
        )

    @override
    async def add_session_to_memory(self, session: Session) -> None:
        """Extracts keywords from session events and stores them in `users/{uid}/memories`."""
        batch = self.client.batch()
        count = 0

        source_session_id = session.id
        project_id = getattr(session, "project_id", None)
        if project_id is None and hasattr(session, "state") and isinstance(session.state, dict):
            project_id = session.state.get("projectId") or session.state.get(
                "temp:coach_project_id"
            )

        for event in session.events:
            if not event.content or not event.content.parts:
                continue

            text = " ".join([part.text for part in event.content.parts if part.text])
            if not text:
                continue

            keywords = self._extract_keywords(text)
            if not keywords:
                continue

            doc_ref = self._user_memories_ref(session.user_id).document()
            batch.set(
                doc_ref,
                {
                    "appName": session.app_name,
                    "userId": session.user_id,
                    "sourceSessionId": source_session_id,
                    "projectId": project_id,
                    "keywords": list(keywords),
                    "author": event.author,
                    "content": event.content.model_dump(exclude_none=True, mode="json"),
                    "timestamp": event.timestamp,
                },
            )
            count += 1
            if count >= 500:
                await batch.commit()
                batch = self.client.batch()
                count = 0

        if count > 0:
            await batch.commit()

    @override
    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Event],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Adds explicit events to memory under `users/{uid}/memories`."""
        batch = self.client.batch()
        count = 0
        metadata = dict(custom_metadata or {})
        project_id = metadata.get("projectId")

        for event in events:
            if not event.content or not event.content.parts:
                continue

            text = " ".join([part.text for part in event.content.parts if part.text])
            if not text:
                continue

            keywords = self._extract_keywords(text)
            if not keywords:
                continue

            doc_ref = self._user_memories_ref(user_id).document()
            batch.set(
                doc_ref,
                {
                    "appName": app_name,
                    "userId": user_id,
                    "sourceSessionId": session_id,
                    "projectId": project_id,
                    "keywords": list(keywords),
                    "author": event.author,
                    "content": event.content.model_dump(exclude_none=True, mode="json"),
                    "timestamp": event.timestamp,
                    "customMetadata": metadata,
                },
            )
            count += 1
            if count >= 500:
                await batch.commit()
                batch = self.client.batch()
                count = 0

        if count > 0:
            await batch.commit()

    @override
    async def add_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        memories: Sequence[MemoryEntry],
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        """Adds explicit memory entries to `users/{uid}/memories`."""
        batch = self.client.batch()
        count = 0
        metadata = dict(custom_metadata or {})

        for memory in memories:
            if not memory.content or not memory.content.parts:
                continue

            text = " ".join([part.text for part in memory.content.parts if part.text])
            if not text:
                continue

            keywords = self._extract_keywords(text)
            tags = memory.custom_metadata.get("tags") or metadata.get("tags")
            if isinstance(tags, (list, tuple, set)):
                for tag in tags:
                    keywords.update(self._extract_keywords(str(tag)))

            if not keywords:
                continue

            merged_metadata = {**metadata, **memory.custom_metadata}
            source_session_id = merged_metadata.get("sourceSessionId")
            project_id = merged_metadata.get("projectId")

            doc_ref = self._user_memories_ref(user_id).document()
            batch.set(
                doc_ref,
                {
                    "appName": app_name,
                    "userId": user_id,
                    "sourceSessionId": source_session_id,
                    "projectId": project_id,
                    "keywords": list(keywords),
                    "author": memory.author or "user",
                    "content": memory.content.model_dump(exclude_none=True, mode="json"),
                    "timestamp": memory.timestamp,
                    "customMetadata": merged_metadata,
                },
            )
            count += 1
            if count >= 500:
                await batch.commit()
                batch = self.client.batch()
                count = 0

        if count > 0:
            await batch.commit()

    @override
    async def _search_by_keyword(
        self, app_name: str, user_id: str, keyword: str
    ) -> list[MemoryEntry]:
        """Searches for memory entries matching a keyword in `users/{uid}/memories`."""
        query = (
            self._user_memories_ref(user_id)
            .where(filter=FieldFilter("appName", "==", app_name))
            .where(filter=FieldFilter("keywords", "array_contains", keyword))
        )

        docs = await query.get()
        entries: list[MemoryEntry] = []
        for doc in docs:
            data = doc.to_dict()
            if data and "content" in data:
                try:
                    content = types.Content.model_validate(data["content"])
                    custom_metadata = dict(data.get("customMetadata") or {})
                    if data.get("sourceSessionId"):
                        custom_metadata["sourceSessionId"] = data["sourceSessionId"]
                    if data.get("projectId"):
                        custom_metadata["projectId"] = data["projectId"]

                    raw_ts = data.get("timestamp")
                    formatted_ts = (
                        _utils.format_timestamp(raw_ts)
                        if isinstance(raw_ts, (int, float))
                        else str(raw_ts or "")
                    )

                    entries.append(
                        MemoryEntry(
                            id=doc.id,
                            content=content,
                            author=data.get("author", ""),
                            timestamp=formatted_ts,
                            custom_metadata=custom_metadata,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse memory entry: {e}")

        return entries


__all__ = ["MEMORIES_COLLECTION", "USERS_COLLECTION", "CoachMemoryService"]
