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

    `draft` is where every task starts: on the board, with no plan yet. It leaves for
    `not_started` when it acquires items or subtasks, which is a derivation rather than a
    user action — see `coach.services.rollups.derive_state`.

    `in_progress` replaced `current` at M4 and **is not singular**. `current` was one task
    per project by construction; that was a claim about the learner's attention the data
    could not keep, and starting a second task silently threw away the fact that the first
    was half-done. What the board pins as "Next up" is now derived (`compute_next_up`)
    rather than enforced.
    """

    DRAFT = "draft"
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
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
GuidanceLevel = Literal["mostly_guided", "balanced", "mostly_unguided"]
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
    guidance_level: GuidanceLevel | None = None
    research_depth: ResearchDepth | None = None
    allow_videos: bool | None = None
    #: Whether `complete_task_item` asks the learner before ticking a step.
    #:
    #: On by default, because completing the last step completes the task and that is the
    #: click docs/10-risks.md Q1 rests on. Off is for a project of short, obvious tasks,
    #: where a dialog per step is friction rather than a safeguard — and it is a *project*
    #: preference rather than a global one for exactly that reason: the same learner can
    #: want the gate on a research project and off on a drill.
    confirm_item_completion: bool | None = None
    preferred_sources: list[str] | None = None
    avoid_sources: list[str] | None = None


class EffectivePrefs(DomainModel):
    """The resolved global ⊕ project view, served by `GET /api/projects/{id}/effective-prefs`.

    One source of truth for the UI, the API, and (from M3) the agent's prompt builder.
    """

    default_task_minutes: Minutes
    guidance_style: GuidanceStyle
    guidance_level: GuidanceLevel = "balanced"
    verbosity: Verbosity
    timezone: str
    research_depth: ResearchDepth
    allow_videos: bool
    confirm_item_completion: bool
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

    Written only by the `update_learner_profile` tool (M7) and by the Settings UI —
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
    """docs/02-data-model.md#usage-quotas-m8-quotas.

    The two points fields are kept numerically equal to `plans/free`'s document so that
    a missing preset (an unseeded emulator, chiefly) falls back to the same numbers a
    seeded one would hand out — see `PlanRepository.get_preset`.
    """

    autonomous_runs_per_day: int = 20
    monthly_points: int = 500
    four_hour_points: int = 80


class Plan(DomainModel):
    """Billing hook. Out of scope for v1; the shape is reserved so it need not be
    retrofitted onto existing documents later."""

    tier: str = "free"
    limits: PlanLimits = Field(default_factory=PlanLimits)


class UsagePoints(DomainModel):
    """One user's spend so far in each of the two windows (`GET /api/me`'s `usage`,
    the scheduler's points guard, and `QuotaService`'s pre-flight check all read this)."""

    monthly: int = 0
    four_hour: int = 0

    def exhausted_window(self, limits: PlanLimits) -> str | None:
        """Which window is spent, or `None` if both still have room.

        Checked in a fixed order — monthly, then 4-hour — only because that is the order a
        human reads the two numbers in; a caller refused for any reason gets the same `429`
        regardless of which window named it.
        """
        if self.monthly >= limits.monthly_points:
            return "monthly"
        if self.four_hour >= limits.four_hour_points:
            return "4-hour"
        return None


class UsageWindow(DomainModel):
    """One window's spend, limit, and reset time — `GET /api/me`'s
    `usage.{monthly,fourHour}`. Not a stored shape; assembled by `QuotaService.status` on
    every call."""

    spent: int
    limit: int
    resets_at: datetime


class UsageStatus(DomainModel):
    monthly: UsageWindow
    four_hour: UsageWindow


class CouponLimits(DomainModel):
    """What a coupon grants: a replacement for the two points fields only. Deliberately
    not `PlanLimits` — a coupon is about spend, not about `autonomousRunsPerDay` pacing, and
    a coupon document that carried a field it never touches would look like it did."""

    monthly_points: int
    four_hour_points: int


class Coupon(DomainModel):
    """`coupons/{code}` — docs/02-data-model.md#couponscode. The document id is the code
    itself, so `code` here is restated for convenience rather than being the only place it
    lives."""

    code: str
    claimed: bool = False
    claimed_by_uid: str | None = None
    claimed_at: datetime | None = None
    limits: CouponLimits
    created_at: datetime | None = None


class User(DomainModel):
    uid: str
    email: str | None = None
    display_name: str | None = None
    #: Once true, `UserService.get_or_create`'s refresh-from-token loop stops touching
    #: `display_name` — set by `PATCH /api/me`, never by the sign-in token's own claim.
    #: Without this a learner's chosen name would be silently overwritten by their
    #: Google account's name on their very next request.
    display_name_customized: bool = False
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
    #: The intake session `POST /api/projects` opens (docs/04-api-contract.md). A pointer
    #: rather than a query, because the alternative is a collection-group scan of every
    #: session in the project to find the one with `taskId: null` — and that is a read on
    #: every visit to the workspace, for a value that never changes after creation.
    #: `SessionService.get_or_create_intake` falls back to the scan when it is absent,
    #: which is what makes projects created before M3 keep working.
    intake_session_id: str | None = None
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


class TaskItem(DomainModel):
    """One thing that has to happen for a leaf task to be done.

    docs/02-data-model.md#task-items. The list is ordered — array position is the order the
    work happens in — and unnumbered, because a re-run of research replaces it and "step 3
    of 7" becoming "step 3 of 5" reads as work disappearing rather than as a better plan.

    `guided` is a routing decision, not a difficulty rating: an unguided item's work happens
    outside the conversation (read this, watch that), so the coach hands it over and waits,
    while a guided one is the teaching the coach does *with* the learner.

    `details` is asymmetric between the two and this is the field's whole point. On an
    unguided item it is the instruction, and the UI renders it. On a guided one it is the
    coach's teaching notes — the exercise's answer lives in there — and the UI must **not**
    render it (docs/06-frontend.md).
    """

    item_id: str
    short_description: str = Field(min_length=1, max_length=300)
    details: str = ""
    guided: bool = False
    completed: bool = False
    completed_at: datetime | None = None
    #: What the item costs, carried over from the report item it was promoted from. `None`
    #: on a hand-added item; the budget meter sums what it has.
    minutes: Minutes | None = None
    #: What an unguided item points at, and half of the identity a re-run matches on.
    url: str | None = None
    #: The report that contributed this item, or `None` for one the learner added by hand.
    #: A hand-added item is never dropped by a re-run.
    source_report_id: str | None = None


class Task(DomainModel):
    id: str
    #: Denormalized for collection-group queries.
    project_id: str
    owner_uid: str
    #: Nesting is exactly one level deep: a subtask cannot have subtasks.
    parent_task_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    state: TaskState = TaskState.DRAFT
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
    #: Set iff `research_status is PENDING`: when the *learner* asked for research, as
    #: opposed to the coach signing the task up for it with `needs_research`. Non-null is
    #: the priority flag and its value is the fairness order among queued tasks, which is
    #: why it is a timestamp and not a boolean
    #: (docs/05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them).
    research_requested_at: datetime | None = None
    latest_report_id: str | None = None
    #: LEAF tasks only. `items` and `rollup` are the same field in two moods — a leaf's plan
    #: is its checklist, a parent's is its subtasks — and are mutually exclusive by
    #: construction rather than by a validator: `split_task` refuses a task that already has
    #: items, which is the only way a leaf becomes a parent.
    items: list[TaskItem] = Field(default_factory=list)
    rollup: Rollup | None = None
    origin: Origin = Origin.USER
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


class TaskWithSubtasks(Task):
    """A parent task with its children nested, as `GET /api/projects/{id}/tasks` returns."""

    subtasks: list[Task] = Field(default_factory=list)


# --------------------------------------------------------------------------------------
# Research reports
# --------------------------------------------------------------------------------------


class ReportItemKind(StrEnum):
    ARTICLE = "article"
    VIDEO = "video"
    EXERCISE = "exercise"
    DOC = "doc"
    CODE_SCAFFOLD = "code_scaffold"


#: Kinds whose work happens *in* the conversation, and which therefore promote to a guided
#: task item. The rest are material the learner goes away and consumes. The model may
#: override per item; this is what a report that says nothing about guidance falls back to,
#: so that a silent report still produces a sensible checklist
#: (docs/02-data-model.md#projectsprojectidresearch_reportsreportid).
GUIDED_KINDS = frozenset({ReportItemKind.EXERCISE, ReportItemKind.CODE_SCAFFOLD})

ItemSource = Literal["youtube", "web", "generated"]
ItemFeedback = Literal["up", "down"]


class ReportItem(DomainModel):
    """One recommendation. `required[]` entries are promoted into `tasks/{id}.items[]`."""

    item_id: str
    kind: ReportItemKind
    title: str = Field(min_length=1, max_length=300)
    url: str | None = None
    minutes: Minutes
    #: Why this is needed for THIS task, in the second person and the learner's own terms.
    #: It becomes the promoted item's `shortDescription`, which is why a `why` reading
    #: "provides necessary background" produces a checklist nobody can act on.
    why: str = ""
    #: The body of an item the coach authored rather than found — the exercise itself, the
    #: scaffold, the questions to ask. Becomes the promoted item's `details`, which for a
    #: guided item is the coach's teaching notes and is never rendered to the learner
    #: (docs/06-frontend.md). Empty for a link, where the title and the URL *are* the
    #: instruction.
    details: str = ""
    source: ItemSource = "web"
    meta: dict[str, str] = Field(default_factory=dict)
    #: `None` means "take the default for this kind" — see `GUIDED_KINDS`.
    guided: bool | None = None

    @property
    def is_guided(self) -> bool:
        return self.kind in GUIDED_KINDS if self.guided is None else self.guided


class Citation(DomainModel):
    uri: str
    title: str = ""


class ReportProgress(DomainModel):
    """User-owned, never written by the agent.

    Holds only `feedback` from M4. `completedItemIds` used to live here and moved to
    `tasks/{id}.items[]`: two reports for one task meant two checklists, and neither was the
    answer to "what is left to do on this task". A thumbs-down is a judgement about *this
    recommendation* and does belong here, because it has to stay attached to the
    recommendation when a re-run supersedes it (docs/02-data-model.md).
    """

    feedback: dict[str, ItemFeedback] = Field(default_factory=dict)


class ResearchReport(DomainModel):
    """`projects/{projectId}/research_reports/{reportId}`.

    Immutable once written, apart from `progress`. Reports accumulate per task rather than
    replacing each other (docs/10-risks.md Q4); the task's checklist does not, and is
    replaced by each run.
    """

    id: str
    project_id: str
    owner_uid: str
    #: `None` since M8: research kicked off from the project coach's own conversation,
    #: about the project as a whole rather than one task. Nothing is promoted into any
    #: task's `items[]` for such a report (docs/02-data-model.md#task-items).
    task_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    summary: str = ""
    required: list[ReportItem] = Field(default_factory=list)
    optional: list[ReportItem] = Field(default_factory=list)
    total_required_minutes: int = 0
    budget_minutes: int = 45
    citations: list[Citation] = Field(default_factory=list)
    progress: ReportProgress = Field(default_factory=ReportProgress)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --------------------------------------------------------------------------------------
# Autonomous runs — the durable job ledger
# --------------------------------------------------------------------------------------


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED_OWNER_PRESENT = "skipped_owner_present"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStep(DomainModel):
    id: str
    status: StepStatus = StepStatus.PENDING
    started_at: datetime | None = None
    ended_at: datetime | None = None
    output: dict[str, Any] | None = None
    error: str | None = None


class RunChange(DomainModel):
    """One reversible write a run made, recorded so undo does not have to guess.

    docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change: "the ledger records
    enough to reverse: created task ids and previous `order`/`nextUpTaskId` values". A
    diff computed at undo time could not tell the run's writes from the learner's, which
    is the whole reason this is recorded forwards rather than reconstructed backwards.
    """

    kind: Literal["task_created", "task_reordered"]
    task_id: str
    #: The `order` the task held *before* the run moved it. `None` on a creation.
    previous_order: str | None = None


class AutonomousRun(DomainModel):
    """`autonomous_runs/{runId}`. docs/05-autonomous-runs.md#run-ledger.

    **M4 writes this document; M5 schedules it.** A manual research run carries
    `trigger: "manual"`, `mode: "inline"`, a `turnId`, and only the two steps M4 has
    implemented — `research` and `post_report`. The board-reshaping steps (`propose_tasks`,
    `reprioritize`) and the selection step are *absent* rather than `pending`, so that
    `cursor` — "first non-complete step" — stays truthful and M5's executor does not
    inherit a backlog of runs it believes it left half-finished.
    """

    id: str
    owner_uid: str
    project_id: str
    task_id: str | None = None
    #: `"requested"` is the learner's queued, headless run: it skips the presence guard,
    #: the cooldown, `autonomousEnabled`, and quiet hours, and it sorts ahead of every
    #: `"scheduled"` candidate
    #: (docs/05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them).
    #: `"manual"` remains the inline run a turn streams.
    trigger: Literal["scheduled", "requested", "manual"] = "manual"
    mode: Literal["queued", "inline"] = "inline"
    status: RunStatus = RunStatus.PENDING
    attempts: int = 1
    max_attempts: int = 3
    lease_expires_at: datetime | None = None
    instance_id: str = ""
    steps: list[RunStep] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    turn_id: str | None = None
    #: What the "Updated by your coach" banner lists and what `POST /api/runs/{id}/undo`
    #: reverses. Appended as the run writes, never derived afterwards.
    changes: list[RunChange] = Field(default_factory=list)
    #: `project.nextUpTaskId` before `reprioritize` touched the board. Recorded because
    #: docs/05-autonomous-runs.md asks the ledger to hold enough to reverse the run — but
    #: undo does not *write* it: the pointer became derived from the board at M4, so
    #: restoring the task's `order` restores the pin as a consequence. It is here as the
    #: audit answer to "what was next up before my coach touched this".
    previous_next_up_task_id: str | None = None
    undone_at: datetime | None = None
    #: Since M8: the research step's own dedicated session — never the task's conversation
    #: session (docs/02-data-model.md#sessions--events-adk-owned-layout). Written once,
    #: the same way `turn_id` is, when the session is created.
    session_id: str | None = None
    #: Also the Firestore TTL field (30 days), touched at every step boundary.
    created_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def cursor(self) -> str | None:
        """The first non-complete step — where a resumed executor picks up."""
        for step in self.steps:
            if step.status is not StepStatus.COMPLETE:
                return step.id
        return None


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
