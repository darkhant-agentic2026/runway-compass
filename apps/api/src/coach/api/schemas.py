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
    AutonomousRun,
    EffectivePrefs,
    GlobalPrefs,
    GuidanceLevel,
    GuidanceStyle,
    ItemFeedback,
    LearnerProfile,
    Minutes,
    Plan,
    Project,
    ProjectStatus,
    ResearchDepth,
    ResearchReport,
    SessionSummary,
    Task,
    TaskState,
    TaskWithSubtasks,
    TurnStatus,
    UsageStatus,
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
    #: docs/02-data-model.md#usage-quotas-m8-quotas. Spend, limit, and reset time for both
    #: windows, regardless of whether either is exhausted.
    usage: UsageStatus


class MeIdentityPatch(RequestModel):
    """`PATCH /api/me`. Only `displayName` today — the one identity field a learner may
    override; `email` and `photoUrl` stay Identity Platform's to set."""

    display_name: str = Field(min_length=1, max_length=100)


class CouponClaimRequest(RequestModel):
    code: str = Field(min_length=1, max_length=64)


class CouponClaimResponse(ResponseModel):
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


class TechnologyBeliefPatch(RequestModel):
    name: str
    level: str
    evidence: str = ""


class LearnerProfilePatch(RequestModel):
    thinking_style: str | None = Field(default=None, max_length=500)
    strengths: list[str] | None = None
    gaps: list[str] | None = None
    technologies: list[TechnologyBeliefPatch] | None = None
    pacing: str | None = None
    feedback_notes: list[str] | None = None


# --- projects ------------------------------------------------------------------------


class ProjectCreate(RequestModel):
    title: str = Field(min_length=1, max_length=200)
    goal: str = ""


class ProjectPrefsPatch(RequestModel):
    default_task_minutes: Minutes | None = None
    guidance_style: GuidanceStyle | None = None
    guidance_level: GuidanceLevel | None = None
    research_depth: ResearchDepth | None = None
    allow_videos: bool | None = None
    confirm_item_completion: bool | None = None
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
    estimated_minutes: Minutes | None = None
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


# `SubtaskDraft`/`TaskSplit` and `POST /api/tasks/{id}/split` were removed after M4. A
# subtask is now created by `POST /api/projects/{id}/tasks` with a `parentTaskId`, one at a
# time — see the note in `services/tasks.py` for why the all-at-once shape had to go.


class TaskItemDraft(RequestModel):
    """One checklist item, as a client or a tool supplies it.

    No `itemId`: it is assigned server-side, like a report item's
    (docs/02-data-model.md#task-items). No `completed` either — an item is added as
    outstanding work, and ticking it is a `PATCH` on the item it became.
    """

    short_description: str = Field(min_length=1, max_length=300)
    details: str = ""
    guided: bool = False
    minutes: Minutes | None = None
    url: str | None = None


class TaskItemsAdd(RequestModel):
    items: list[TaskItemDraft] = Field(min_length=1, max_length=30)


class TaskItemPatch(RequestModel):
    completed: bool | None = None
    short_description: str | None = Field(default=None, min_length=1, max_length=300)
    details: str | None = None
    guided: bool | None = None


class TaskItemReorder(RequestModel):
    """Exactly one of the two must be given; the service enforces that."""

    after_item_id: str | None = None
    before_item_id: str | None = None


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
    #: docs/04-api-contract.md: `GET /api/tasks/{id}` returns "Task + `items[]` + subtasks
    #: + `latestReport`". The checklist is on the task; the report is the material behind
    #: it, and the workspace renders `optional[]` and the citations from here.
    latest_report: ResearchReport | None = None


class RunResponse(ResponseModel):
    """`GET /api/runs/{runId}` — the whole ledger row.

    Whole rather than projected: the client renders `steps[]` as progress, keys the
    "Updated by your coach" banner off `changes[]` and `undoneAt`, and reads `turnId` to
    attach to the stream. Picking a subset here would mean a second endpoint the first
    time the UI wanted one more field.
    """

    run: AutonomousRun


class RunListResponse(ResponseModel):
    runs: list[AutonomousRun]


class RunUndoResponse(ResponseModel):
    """`POST /api/runs/{runId}/undo`.

    `taskIds` is what the client invalidates. It is returned rather than inferred from
    `run.changes` because undo tolerates a task that has already gone, so what it *did*
    touch is a strictly smaller list than what the run changed.
    """

    run: AutonomousRun
    task_ids: list[str]


class ReportListResponse(ResponseModel):
    """`GET /api/tasks/{id}/reports` — newest first.

    A list because reports accumulate rather than replacing each other
    (docs/10-risks.md Q4); the UI renders the newest and collapses the rest.
    """

    reports: list[ResearchReport]


class ReportItemFeedback(RequestModel):
    """`PATCH /api/reports/{reportId}/items/{itemId}`.

    Feedback and nothing else. This body used to carry `completed` as well; item completion
    moved onto the task at M4 (docs/04-api-contract.md#tasks), and the field is *absent*
    rather than ignored so a client still sending one gets a 422 instead of a silent
    success. `taskId` is required because a report is addressed without its project and
    ownership is checked through the task.
    """

    task_id: str
    feedback: ItemFeedback | None = None


class ReportResponse(ResponseModel):
    report: ResearchReport


class ResearchRequest(RequestModel):
    reason: str = Field(default="", max_length=2000)
    budget_minutes_override: Minutes | None = None
    #: Re-run even when the task already has materials.
    force: bool = False
    #: + M8: files the learner attached to the question, forwarded into the research
    #: session's opening turn exactly as `TurnRequest.attachments` are — an upload is
    #: addressable by `uploadId` from any session, so this needs no new plumbing beyond
    #: accepting the same shape here.
    attachments: list[TurnAttachment] = Field(default_factory=list)


class ResearchResponse(ResponseModel):
    """The 202 from `POST /api/sessions/{sid}/research`.

    `turnId` is what the client subscribes to. `sessionId` (+ M8) is the run's own fresh
    session — never the `sid` the request was made against — and is what the research view
    reads to render that run's transcript (docs/04-api-contract.md#post-apisessionssidresearch).
    """

    run_id: str
    turn_id: str | None
    session_id: str
    mode: str


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


class TurnConfirmation(RequestModel):
    """The learner's answer to a tool that asked before acting.

    `discard_task` is gated by ADK's `require_confirmation` (docs/03-agent-design.md), so
    the turn that proposes it ends with an `adk_request_confirmation` function call and
    resumes only when a matching function *response* arrives. This is that response,
    shaped as fields rather than as a raw ADK part: the client should not be building ADK
    payloads, and the call id is the only thing it has to carry back.

    `payload` carries a *structured* answer, which is what makes `ask_learner` possible:
    ADK's `ToolConfirmation` has a free-form `payload` beside `confirmed`, so a tool can
    ask a question rather than only for approval and get the selection back through the
    same handshake. It is `None` for the yes/no gates, whose whole answer is `confirmed`.

    Untyped here on purpose. The shape belongs to the tool that asked — `ask_learner`
    validates the selection against the options it offered, which is where the check has
    to be anyway, since the payload has been through the client.
    """

    function_call_id: str
    confirmed: bool
    payload: dict[str, Any] | None = None


class TurnRequest(RequestModel):
    """`POST /api/sessions/{sid}/turns`.

    `idempotencyKey` is in the contract's example body; it is accepted here *and* as the
    `Idempotency-Key` header, which is what `IdempotencyMiddleware` actually reads. The
    body field is kept so a client following the contract's example is not silently
    unprotected.
    """

    text: str = ""
    attachments: list[TurnAttachment] = Field(default_factory=list)
    confirmation: TurnConfirmation | None = None
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
