"""Request and response bodies.

Wire shapes from docs/04-api-contract.md. Responses reuse the domain models directly —
they already serialize to camelCase — so there is no second definition of a task to keep
in step. Requests get their own models because they are patches and partials, where the
difference between "absent" and "null" matters.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from coach.services.models import (
    EffectivePrefs,
    GlobalPrefs,
    GuidanceStyle,
    LearnerProfile,
    Minutes,
    Plan,
    Project,
    ProjectStatus,
    ResearchDepth,
    SessionSummary,
    Task,
    TaskState,
    TaskWithSubtasks,
    TurnStatus,
    Verbosity,
)


class RequestModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ResponseModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --- identity & preferences ----------------------------------------------------------


class MeResponse(ResponseModel):
    uid: str
    email: str | None
    display_name: str | None
    photo_url: str | None
    global_prefs: GlobalPrefs
    learner_profile: LearnerProfile
    plan: Plan


class QuietHoursPatch(RequestModel):
    start: str
    end: str


class GlobalPrefsPatch(RequestModel):
    """`PATCH /api/me/prefs`. Every field optional; absent means "leave alone"."""

    default_task_minutes: Minutes | None = None
    guidance_style: GuidanceStyle | None = None
    verbosity: Verbosity | None = None
    timezone: str | None = None
    autonomous_enabled: bool | None = None
    autonomous_quiet_hours: QuietHoursPatch | None = None


class LearnerProfilePatch(RequestModel):
    thinking_style: str | None = Field(default=None, max_length=500)
    strengths: list[str] | None = None
    gaps: list[str] | None = None
    pacing: str | None = None


# --- projects ------------------------------------------------------------------------


class ProjectCreate(RequestModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""


class ProjectPrefsPatch(RequestModel):
    default_task_minutes: Minutes | None = None
    guidance_style: GuidanceStyle | None = None
    research_depth: ResearchDepth | None = None
    allow_videos: bool | None = None
    preferred_sources: list[str] | None = None
    avoid_sources: list[str] | None = None


class ProjectPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    goal: str | None = None
    status: ProjectStatus | None = None
    prefs: ProjectPrefsPatch | None = None


class ProjectListResponse(ResponseModel):
    projects: list[Project]


class BoardResponse(ResponseModel):
    """`GET /api/projects/{id}/tasks` — parents with nested subtasks."""

    tasks: list[TaskWithSubtasks]


# --- tasks ---------------------------------------------------------------------------


class TaskCreate(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    estimated_minutes: Minutes = 45
    parent_task_id: str | None = None
    after_task_id: str | None = None
    needs_research: bool = True


class TaskPatch(RequestModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = None
    estimated_minutes: Minutes | None = None
    needs_research: bool | None = None


class TaskStateChange(RequestModel):
    state: TaskState
    postponed_until: datetime | None = None


class TaskReorder(RequestModel):
    """Exactly one of the two must be given; the service enforces that."""

    after_task_id: str | None = None
    before_task_id: str | None = None


class SubtaskDraft(RequestModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    estimated_minutes: Minutes
    needs_research: bool = True


class TaskSplit(RequestModel):
    subtasks: list[SubtaskDraft] = Field(min_length=2, max_length=8)


class ProjectDerived(ResponseModel):
    """The project fields a task mutation can move."""

    id: str
    next_up_task_id: str | None
    counts: dict[str, int]


class TaskMutationResponse(ResponseModel):
    """What every task mutation returns.

    docs/04-api-contract.md: "Reorder and state changes return the full updated task
    (plus the affected parent) so the client can reconcile optimistically without a
    refetch." `project` is carried alongside for the same reason — `counts` and
    `nextUpTaskId` move on the same write, and without them the board's "Next up" pin
    would need a second round-trip to settle.
    """

    task: Task
    parent: Task | None = None
    project: ProjectDerived | None = None


class TaskDetailResponse(ResponseModel):
    task: TaskWithSubtasks


class EffectivePrefsResponse(ResponseModel):
    project_id: str
    effective_prefs: EffectivePrefs


# --- sessions & turns ------------------------------------------------------------------


class SessionResponse(ResponseModel):
    session: SessionSummary


class SessionEventView(ResponseModel):
    """One transcript row.

    `event` is the serialized ADK `Event` verbatim, not a projection of it. The transcript
    is ADK's data (docs/02-data-model.md nests the whole event under `event_data`), and
    re-shaping it here would mean a second definition of a conversation turn that has to
    be kept in step with a pinned dependency's model.
    """

    seq: int
    event_id: str
    event: dict[str, Any]


class SessionEventsResponse(ResponseModel):
    events: list[SessionEventView]
    next_after_seq: int
    has_more: bool


class TurnAttachment(RequestModel):
    upload_id: str
    mime_type: str


class TurnRequest(RequestModel):
    """`POST /api/sessions/{sid}/turns`.

    `idempotencyKey` is in the contract's example body; it is accepted here *and* as the
    `Idempotency-Key` header, which is what `IdempotencyMiddleware` actually reads. The
    body field is kept so a client following the contract's example is not silently
    unprotected.
    """

    text: str = ""
    attachments: list[TurnAttachment] = Field(default_factory=list)
    idempotency_key: str | None = None


class TurnAcceptedResponse(ResponseModel):
    turn_id: str
    session_id: str
    status: TurnStatus
    start_seq: int = 0


class TurnStatusResponse(ResponseModel):
    turn_id: str
    status: TurnStatus
    last_seq: int


class WsTicketResponse(ResponseModel):
    ticket: str
    expires_at: datetime


# --- uploads ---------------------------------------------------------------------------


class UploadCreate(RequestModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str
    size_bytes: int = Field(gt=0)


class UploadCreated(ResponseModel):
    upload_id: str
    signed_url: str


class UploadFinalized(ResponseModel):
    upload_id: str
    mime_type: str
