"""Domain models.

These are the shapes of docs/02-data-model.md, one model per document. Field names are
snake_case in Python and camelCase on the wire, via a single alias generator — the same
serialization feeds Firestore documents and REST responses, so a field cannot be named
one thing in the database and another in the API by accident.

`services/` owns these types; `repositories/` maps them to and from Firestore documents
and knows the collection paths. `api/` re-exposes them in request/response schemas.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Minutes = Annotated[int, Field(ge=1, le=24 * 60)]

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def snake_case(camel: str) -> str:
    """`defaultTaskMinutes` -> `default_task_minutes`.

    Patches arrive keyed by wire name and the in-memory models are keyed by field name;
    this converts one to the other so a patched model can be returned without a re-read.
    """
    return _CAMEL_BOUNDARY.sub("_", camel).lower()


class DomainModel(BaseModel):
    """Base with camelCase aliases and construction by either name."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        use_enum_values=False,
    )

    def to_document(self, *, exclude_none: bool = False) -> dict[str, Any]:
        """The Firestore representation: camelCase keys, enums as their string values."""
        return self.model_dump(by_alias=True, mode="python", exclude_none=exclude_none)


# --------------------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------------------


class TaskState(StrEnum):
    """docs/02-data-model.md#task-state-machine.

    `postponed` and `postponed_until` are distinct states, not one state with an optional
    field: the first waits for the user, the second waits for a clock.
    """

    NOT_STARTED = "not_started"
    CURRENT = "current"
    COMPLETED = "completed"
    POSTPONED = "postponed"
    POSTPONED_UNTIL = "postponed_until"
    DISCARDED = "discarded"


class ResearchStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class Origin(StrEnum):
    USER = "user"
    AGENT = "agent"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


GuidanceStyle = Literal["socratic", "direct", "mixed"]
Verbosity = Literal["terse", "balanced", "thorough"]
ResearchDepth = Literal["light", "standard", "deep"]


# --------------------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------------------


class QuietHours(DomainModel):
    start: str = "23:00"
    end: str = "07:00"


class GlobalPrefs(DomainModel):
    """`users/{uid}.globalPrefs`. Every field has a value — this is the fallback layer."""

    default_task_minutes: Minutes = 45
    guidance_style: GuidanceStyle = "socratic"
    verbosity: Verbosity = "balanced"
    timezone: str = "UTC"
    autonomous_enabled: bool = True
    autonomous_quiet_hours: QuietHours = Field(default_factory=QuietHours)


class ProjectPrefs(DomainModel):
    """`projects/{projectId}.prefs`.

    A `None` field means "inherit from globalPrefs" (docs/02-data-model.md). The
    project-only fields below have no global counterpart, so `None` there means "use the
    documented default" instead — which `resolve_prefs` supplies.
    """

    default_task_minutes: Minutes | None = None
    guidance_style: GuidanceStyle | None = None
    research_depth: ResearchDepth | None = None
    allow_videos: bool | None = None
    preferred_sources: list[str] | None = None
    avoid_sources: list[str] | None = None


class EffectivePrefs(DomainModel):
    """The resolved global ⊕ project view, served by `GET /api/projects/{id}/effective-prefs`.

    One source of truth for the UI, the API, and (from M3) the agent's prompt builder.
    """

    default_task_minutes: Minutes
    guidance_style: GuidanceStyle
    verbosity: Verbosity
    timezone: str
    research_depth: ResearchDepth
    allow_videos: bool
    preferred_sources: list[str]
    avoid_sources: list[str]


# --------------------------------------------------------------------------------------
# User
# --------------------------------------------------------------------------------------


class TechnologyBelief(DomainModel):
    name: str
    level: str
    evidence: str = ""


class LearnerProfile(DomainModel):
    """Agent-maintained, user-editable beliefs about the learner.

    Written only by the `update_learner_profile` tool (M6) and by the Settings UI —
    never by free-form model output.
    """

    thinking_style: str = Field(default="", max_length=500)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    technologies: list[TechnologyBelief] = Field(default_factory=list)
    pacing: str = ""
    #: Capped ring buffer, 20 entries.
    feedback_notes: list[str] = Field(default_factory=list, max_length=20)
    updated_at: datetime | None = None
    updated_by: Literal["agent", "user"] = "user"
    version: int = 0


class PlanLimits(DomainModel):
    autonomous_runs_per_day: int = 20


class Plan(DomainModel):
    """Billing hook. Out of scope for v1; the shape is reserved so it need not be
    retrofitted onto existing documents later."""

    tier: str = "free"
    limits: PlanLimits = Field(default_factory=PlanLimits)


class User(DomainModel):
    uid: str
    email: str | None = None
    display_name: str | None = None
    photo_url: str | None = None
    created_at: datetime | None = None
    last_seen_at: datetime | None = None
    global_prefs: GlobalPrefs = Field(default_factory=GlobalPrefs)
    learner_profile: LearnerProfile = Field(default_factory=LearnerProfile)
    plan: Plan = Field(default_factory=Plan)


# --------------------------------------------------------------------------------------
# Project
# --------------------------------------------------------------------------------------


class ProjectCounts(DomainModel):
    total: int = 0
    completed: int = 0
    open_minutes: int = 0


class Project(DomainModel):
    id: str
    owner_uid: str
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    prefs: ProjectPrefs = Field(default_factory=ProjectPrefs)
    #: Denormalized pointer, maintained transactionally alongside the `current` task.
    next_up_task_id: str | None = None
    counts: ProjectCounts = Field(default_factory=ProjectCounts)
    last_autonomous_run_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --------------------------------------------------------------------------------------
# Task
# --------------------------------------------------------------------------------------


class Rollup(DomainModel):
    """Maintained on PARENT tasks only, in the same transaction as any subtask write.

    Parent cards render counts and summed minutes with no extra reads, which is the
    "card shows number of sub-tasks and total estimated duration" requirement.
    """

    subtask_count: int = 0
    completed_subtasks: int = 0
    total_estimated_minutes: int = 0


class Task(DomainModel):
    id: str
    #: Denormalized for collection-group queries.
    project_id: str
    owner_uid: str
    #: Nesting is exactly one level deep: a subtask cannot have subtasks.
    parent_task_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    state: TaskState = TaskState.NOT_STARTED
    #: Set iff `state == postponed_until`.
    postponed_until: datetime | None = None
    estimated_minutes: Minutes = 45
    actual_minutes: int | None = None
    #: Fractional index; see `coach.services.ordering`.
    order: str
    #: 1:1 with an ADK session, created lazily from M2.
    session_id: str | None = None
    needs_research: bool = True
    research_status: ResearchStatus = ResearchStatus.NONE
    latest_report_id: str | None = None
    rollup: Rollup | None = None
    origin: Origin = Origin.USER
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class TaskWithSubtasks(Task):
    """A parent task with its children nested, as `GET /api/projects/{id}/tasks` returns."""

    subtasks: list[Task] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Turns — the streaming checkpoint ledger
# --------------------------------------------------------------------------------------


class TurnStatus(StrEnum):
    """docs/02-data-model.md#turnsturnid.

    `cancelled` is reachable only from the explicit cancel endpoint — a client
    disconnecting never produces it, which is the whole point of the design
    (docs/04-api-contract.md#surviving-client-disconnects).
    """

    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not TurnStatus.RUNNING


class CheckpointSlice(DomainModel):
    """One flush of the delta buffer.

    `fromSeq`/`toSeq`/`text` are the shape in docs/02-data-model.md. `lengths` is an
    addition, and it is what makes resume exact rather than approximate.

    A slice merges every delta published between two flushes, so a client whose
    `lastSeq` falls *inside* a slice cannot be served by the slice alone: replaying it
    whole would resend text the client already rendered, and skipping it would lose the
    tail. `lengths[i]` is the character count of the delta at `fromSeq + i`, so the
    replay path can cut the string at exactly the right offset. The invariant
    `sum(lengths) == len(text)` and `len(lengths) == toSeq - fromSeq + 1` is asserted by
    the streaming tests.
    """

    from_seq: int
    to_seq: int
    text: str
    lengths: list[int] = Field(default_factory=list)

    def text_after(self, last_seq: int) -> str:
        """The part of this slice a client that has already seen `last_seq` still needs."""
        if last_seq < self.from_seq:
            return self.text
        if last_seq >= self.to_seq:
            return ""
        consumed = sum(self.lengths[: last_seq - self.from_seq + 1])
        return self.text[consumed:]


class TurnError(DomainModel):
    code: str
    message: str
    retryable: bool = False


class Turn(DomainModel):
    """`turns/{turnId}`.

    Owned by the *process*, not by the socket: `instanceId` records which Cloud Run
    instance holds the generation task, and a reconnect landing elsewhere reads this
    document rather than expecting a live broker (docs/04-api-contract.md).
    """

    id: str
    session_id: str
    owner_uid: str
    status: TurnStatus = TurnStatus.RUNNING
    started_at: datetime | None = None
    #: Also the Firestore TTL field (7 days). A turn that never reaches a terminal state
    #: would never expire, which is why the drain and the ledger sweep both set it.
    ended_at: datetime | None = None
    last_seq: int = 0
    instance_id: str = ""
    lease_expires_at: datetime | None = None
    checkpoints: list[CheckpointSlice] = Field(default_factory=list)
    error: TurnError | None = None

    def replay_from(self, last_seq: int) -> list[tuple[int, str]]:
        """`(seq, text)` pairs a client resuming at `last_seq` has not seen yet.

        Trailing slices arrive whole; the one straddling `last_seq` is trimmed. Empty
        results are dropped so a reconnect at the very end of a turn does not emit a
        stream of blank deltas.
        """
        pending: list[tuple[int, str]] = []
        for slice_ in self.checkpoints:
            if slice_.to_seq <= last_seq:
                continue
            text = slice_.text_after(last_seq)
            if text:
                pending.append((slice_.to_seq, text))
        return pending


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


class SessionSummary(DomainModel):
    """`GET /api/sessions/{sid}` — metadata and linkage.

    The session *document* is ADK-owned (docs/02-data-model.md); this is the view of it
    the API contract promises, assembled by `SessionService` rather than stored.
    """

    id: str
    project_id: str | None = None
    task_id: str | None = None


# --------------------------------------------------------------------------------------
# Presence
# --------------------------------------------------------------------------------------


class Presence(DomainModel):
    """`presence/{uid}`.

    "Owner is working here" is `activeProjectId == projectId` and a heartbeat inside the
    window — evaluated by the autonomous tick at M5, written by the WebSocket from M2.
    """

    uid: str
    active_project_id: str | None = None
    active_task_id: str | None = None
    last_heartbeat_at: datetime | None = None
    connections: int = 0
