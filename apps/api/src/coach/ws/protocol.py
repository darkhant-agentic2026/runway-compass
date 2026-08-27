"""The `/ws` frame vocabulary.

docs/04-api-contract.md#websocket-protocol-ws is the specification, verbatim: JSON
frames, every frame has `type`, and server→client frames carrying stream content also
carry `turnId` and `seq`.

Frames are modelled rather than hand-built as dicts so that the field names the frontend
parses with Zod have exactly one definition on this side. `apps/web/src/lib/frames.ts`
mirrors them; the two are kept honest by the streaming tests on both sides.

An unknown client frame `type` is rejected, not ignored — the client is ours and a typo
should be loud. An unknown *server* frame type is ignored by the client
forward-compatibly, which is the asymmetry docs/06-frontend.md asks for.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Frame(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")


# --- client → server -------------------------------------------------------------------


class Subscribe(Frame):
    """`subscribe` takes exactly one of `turnId` or `runId`.

    Subscribing by `runId` is what makes the `409` from
    `POST /api/sessions/{sid}/research` actionable, and a scheduled run has no `turnId`
    at all. Runs arrive at M4/M5; the frame is accepted here so the client's socket
    module does not need a second shape later.
    """

    type: Literal["subscribe"]
    turn_id: str | None = None
    run_id: str | None = None


class Resume(Frame):
    type: Literal["resume"]
    turn_id: str
    last_seq: int = 0


class Unsubscribe(Frame):
    type: Literal["unsubscribe"]
    turn_id: str | None = None
    run_id: str | None = None


class PresenceFrame(Frame):
    """Sent every 30 s while a task workspace is focused.

    The autonomous tick reads the resulting `presence/{uid}` document to decide whether
    the owner is working in a project (docs/05-autonomous-runs.md).
    """

    type: Literal["presence"]
    project_id: str | None = None
    task_id: str | None = None


class Ping(Frame):
    type: Literal["ping"]


ClientFrame = Subscribe | Resume | Unsubscribe | PresenceFrame | Ping


# --- server → client -------------------------------------------------------------------


class TurnStart(Frame):
    type: Literal["turn_start"] = "turn_start"
    turn_id: str
    session_id: str


class Delta(Frame):
    """`author` is ADK's `Event.author` — the root agent for an ordinary chat turn, but
    one of several node names (`research_planner`, `topic_researcher`, …) across a single
    `research_workflow`/`build_roadmap_workflow` turn. The frontend's live stream buffer
    starts a new message segment whenever `author` changes from the previous frame's,
    which is what keeps one multi-agent turn from streaming into a single run-on bubble
    (`stores/stream.ts`)."""

    type: Literal["delta"] = "delta"
    turn_id: str
    seq: int
    text: str
    author: str = ""


class ToolCall(Frame):
    type: Literal["tool_call"] = "tool_call"
    turn_id: str
    seq: int
    name: str
    args_preview: dict[str, Any] = Field(default_factory=dict)
    author: str = ""


class ToolResult(Frame):
    type: Literal["tool_result"] = "tool_result"
    turn_id: str
    seq: int
    name: str
    ok: bool = True
    author: str = ""


class Artifact(Frame):
    type: Literal["artifact"] = "artifact"
    turn_id: str
    seq: int
    kind: str
    report_id: str | None = None
    task_id: str | None = None


class TurnComplete(Frame):
    type: Literal["turn_complete"] = "turn_complete"
    turn_id: str
    seq: int
    event_ids: list[str] = Field(default_factory=list)


class TurnError(Frame):
    type: Literal["turn_error"] = "turn_error"
    turn_id: str
    seq: int
    code: str
    message: str
    retryable: bool = True


class BoardUpdate(Frame):
    """The invalidation push that keeps the board live while the agent works.

    Deliberately carries ids rather than task bodies: the client turns it into a TanStack
    Query invalidation rather than trying to patch state from the message
    (docs/06-frontend.md#the-bridge).
    """

    type: Literal["board_update"] = "board_update"
    project_id: str
    task_ids: list[str] = Field(default_factory=list)
    origin: str = "agent"
    run_id: str | None = None


class RunStatus(Frame):
    type: Literal["run_status"] = "run_status"
    run_id: str
    step: str
    status: str


class Pong(Frame):
    type: Literal["pong"] = "pong"


ServerFrame = (
    TurnStart
    | Delta
    | ToolCall
    | ToolResult
    | Artifact
    | TurnComplete
    | TurnError
    | BoardUpdate
    | RunStatus
    | Pong
)

#: Frames that end a turn's stream. A subscriber that sees one of these can stop
#: listening, and the broker uses the same set to close idle queues.
TERMINAL_TYPES = frozenset({"turn_complete", "turn_error"})


__all__ = [
    "TERMINAL_TYPES",
    "Artifact",
    "BoardUpdate",
    "ClientFrame",
    "Delta",
    "Frame",
    "Ping",
    "Pong",
    "PresenceFrame",
    "Resume",
    "RunStatus",
    "ServerFrame",
    "Subscribe",
    "ToolCall",
    "ToolResult",
    "TurnComplete",
    "TurnError",
    "TurnStart",
    "Unsubscribe",
]
