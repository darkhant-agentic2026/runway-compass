# API Contract

Base URL: same origin as the SPA — the Cloud Run service serves both the static SPA and
these endpoints, so no rewrite layer is involved. All payloads JSON unless noted. Errors
follow RFC 9457 `application/problem+json`.

**Including unhandled ones.** A bug used to be the single thing in the service answering in
`text/plain` — Starlette's default 500 — so a client parsing for a problem document fell
back to the HTTP status text and showed the user "request failed" while the traceback sat
in the logs with nothing tying it to their request. Unhandled exceptions now render as a
problem document carrying a **`traceId`**, taken from Cloud Run's `X-Cloud-Trace-Context`
when present so that the value in the response is the one
`gcloud logging read 'trace:"…"'` matches. `detail` names the exception outside production
and is a fixed string in it, since an exception message can carry a bucket name, a query,
or a row of user data.

## Authentication

- Frontend signs in with **Cloud Identity Platform**, Google provider.
- REST: `Authorization: Bearer <id-token>`; verified with
  `firebase_admin.auth.verify_id_token` — the Admin SDK for `identitytoolkit`, and the only
  reason that dependency exists. Signature, audience, and expiry are checked **offline**
  against cached Google public keys, so there is no network call on the request path.
- **Revocation checking is selective, by decision.** `check_revoked=True` makes
  `verify_id_token` fetch the user record from identitytoolkit — a network round-trip on
  every call it is used for. It is therefore enabled on exactly two endpoints:

  | Path | `check_revoked` | Why |
  | --- | --- | --- |
  | `POST /api/ws-ticket` | `True` | The ticket authorizes a socket that may live for the full 3600 s request timeout, so this one check covers a long-lived credential. |
  | `DELETE /api/me` | `True` | Irreversible and cascading. |
  | everything else | `False` | Offline, zero added latency. |

  Accepted window: a revoked or disabled account keeps REST access until its current ID token
  expires (≤ 1 hour), and keeps an established socket until that socket closes. Widening this
  means paying a round-trip on every request — a poor trade for a single-role app where
  `DELETE /api/me` already removes the underlying data. Enforced by test
  ([08-testing.md](08-testing.md)).
- Local only: when `ENV=local`, the auth dependency additionally accepts
  `Authorization: Bearer dev:<uid>` and builds the `Principal` directly, since Identity
  Platform has no local emulator. This path is inert for every other `ENV` value and has a
  dedicated regression test asserting so ([08-testing.md](08-testing.md)).
- WebSocket: browsers cannot set headers on `new WebSocket()`, so the client first calls
  `POST /api/ws-ticket` (authenticated by ID token, with the revocation check above) to get a
  **single-use, 60-second ticket**, then connects to `wss://…/ws?ticket=…`. The ticket is
  redeemed and deleted server-side on connect. Query-string tickets are safe here because
  they are one-shot and short-lived; long-lived tokens in URLs are not. This handshake is the
  socket's *only* authorization point — nothing re-verifies mid-connection — which is why it
  is one of the two endpoints that pays for a revocation check.
- `/internal/*`: Google-signed OIDC bearer token, verified for issuer, audience (the
  service URL), and `email` equal to the expected scheduler/tasks service account. Cloud
  Run IAM additionally requires `roles/run.invoker`.

## REST endpoints

### Identity & preferences

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/me` | Profile, `globalPrefs`, `learnerProfile`, plan limits |
| `PATCH` | `/api/me/prefs` | Partial update of `globalPrefs` |
| `PATCH` | `/api/me/learner-profile` | User edits/resets agent beliefs; bumps `version` |
| `DELETE` | `/api/me` | Account + data deletion (async cascade job) |
| `POST` | `/api/ws-ticket` | `→ { ticket, expiresAt }` |

### Projects

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/projects` | `?status=active` |
| `POST` | `/api/projects` | `{ title, goal? }` — creates project + an intake session (a session with `taskId: null`) |
| `GET` | `/api/projects/{id}` | Includes `counts`, `nextUpTaskId` |
| `PATCH` | `/api/projects/{id}` | title, goal, status, `prefs` patch |
| `POST` | `/api/projects/{id}/session` | Get-or-create the project's **intake session** (the one with `taskId: null`). Added at M3: `POST /api/projects` creates it, and nothing else resolved a project back to it |
| `GET` | `/api/projects/{id}/effective-prefs` | Resolved global ⊕ project — one source of truth for UI and agent |
| `GET` | `/api/projects/{id}/tasks` | `?include_completed=false&include_discarded=false`; returns parents with nested `subtasks[]` and `rollup` |
| `DELETE` | `/api/projects/{id}` | Soft-delete → `archived` |

### Tasks

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/projects/{id}/tasks` | `{ title, description?, estimatedMinutes, parentTaskId?, afterTaskId? }` — with a `parentTaskId` this creates a **subtask**, and the first one inherits the parent's checklist ([02-data-model.md](02-data-model.md#task-items)) |
| `GET` | `/api/tasks/{id}` | Task + `items[]` + subtasks + `latestReport` |
| `PATCH` | `/api/tasks/{id}` | Fields; `estimatedMinutes` triggers parent rollup recompute |
| `POST` | `/api/tasks/{id}/state` | `{ state, postponedUntil? }` — validated against state machine |
| `POST` | `/api/tasks/{id}/reorder` | `{ afterTaskId } \| { beforeTaskId }` |
| `GET` | `/api/tasks/{id}/reports` | Research reports for the task, newest first |
| `POST` | `/api/tasks/{id}/research-request` | **Queue research** for the next tick to run headless — sets `researchStatus: "pending"` and `researchRequestedAt: now` (below) |
| `DELETE` | `/api/tasks/{id}/research-request` | Cancel a queued request while it is still `pending`; returns the task |
| `POST` | `/api/tasks/{id}/items` | Append items by hand — `{ items: [{ shortDescription, details?, guided? }] }` |
| `PATCH` | `/api/tasks/{id}/items/{itemId}` | `{ completed?, shortDescription?, details?, guided? }` — the checkbox and the inline edit |
| `POST` | `/api/tasks/{id}/items/{itemId}/reorder` | `{ afterItemId } \| { beforeItemId }` — array positions, rewritten whole |
| `DELETE` | `/api/tasks/{id}/items/{itemId}` | Remove an item the learner does not want |
| `PATCH` | `/api/reports/{reportId}/items/{itemId}` | `{ feedback: "up" \| "down" \| null }` — writes `progress.feedback` only, never the report body |

**Item completion is a task endpoint, not a report endpoint** — the M4 change to the earlier
contract. `PATCH /api/reports/{rid}/items/{iid}` used to take `completed` as well; a task's
checklist now lives on the task ([02-data-model.md](02-data-model.md#task-items)) and a
report is the immutable record of one research run. What survives on the report endpoint is
the thumbs-down control ([06-frontend.md](06-frontend.md),
[10-risks.md](10-risks.md#r5--research-quality-and-link-rot)), which is a judgement about a
*recommendation* and has to stay attached to it when a re-run supersedes it. The endpoint
now rejects `completed` outright rather than only on `optional[]` items, and the regression
test in [08-testing.md](08-testing.md) moved with it.

`PATCH /api/tasks/{id}/items/{itemId}` returns the **full updated task**, because a write
that completes the last item also changes the task's `state` and its project's `counts` —
a client that patched only the item into its cache would show a completed checklist above a
task still badged "in progress" until the next refetch.

Item endpoints are refused on a task with subtasks: `items` and `rollup` are mutually
exclusive ([02-data-model.md](02-data-model.md#task-items)).

All mutating endpoints accept `Idempotency-Key`. Reorder and state changes return the
full updated task (plus the affected parent) so the client can reconcile optimistically
without a refetch.

#### `POST` / `DELETE /api/tasks/{id}/research-request`

```jsonc
// POST and DELETE both take no body and both answer with the full updated task,
// as every other task mutation does
{ "task": { "…": "…", "researchStatus": "pending", "researchRequestedAt": "2026-08-20T…" } }
```

No `budgetMinutesOverride` here, unlike the inline trigger: a queued run resolves the
budget when it executes, from the task and the project's effective prefs at that moment,
and carrying an override would mean a fourth research field on the task document to hold it
until the tick arrives.

The learner's **queued, headless** alternative to `POST /api/sessions/{sid}/research`. That
one runs research inline in a turn the caller watches stream; this one marks the task and
returns, and the next `/internal/tick` executes it in the background with priority over
auto-scheduled work — the presence guard, the cooldown, `autonomousEnabled`, and quiet hours
are all skipped for it
([05-autonomous-runs.md](05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them)).

Behaviour:
- Refuses a task with subtasks (`409`) — a composite task's plan is its subtasks, and each
  is researched on its own, which is the same rule the inline trigger already applies.
- Refuses a `discarded` task (`409`). A `completed` one is allowed: queueing research on it
  does not reopen it, and a learner who wants more material on something they finished is
  making a coherent request.
- **Idempotent by shape.** `POST` on a task already `pending` is a `200` that leaves the
  original `researchRequestedAt` intact rather than refreshing it, so a double-click cannot
  send a task to the back of its own queue. `DELETE` on a task that is not `pending` is a
  `200` with the task unchanged — the queue is empty either way, which is what the caller
  asked for.
- `DELETE` does **not** stop a run that has already started. Once the tick picks the task
  up, `researchStatus` is `in_progress` and the request is gone; stopping it is the turn's
  cancel. The UI reflects this by dropping the cancel affordance the moment the status
  leaves `pending`.
- Both verbs push `board_update` with the task's id, because the queued badge renders on
  the board card as well as in the workspace ([06-frontend.md](06-frontend.md#task-board-projectsprojectid)).

### Sessions & turns

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/tasks/{id}/session` | Get-or-create the task's session; returns `sessionId` |
| `GET` | `/api/sessions/{sid}` | Metadata + linkage |
| `GET` | `/api/sessions/{sid}/events` | Paginated by `seq` (`?after_seq=&limit=`), for transcript hydration |
| `POST` | `/api/sessions/{sid}/turns` | Start a turn (below) |
| `POST` | `/api/sessions/{sid}/turns/{turnId}/cancel` | Explicit user cancel — the *only* thing that stops generation |
| `POST` | `/api/sessions/{sid}/research` | **Manual research trigger** (below) |
| `GET` | `/api/turns/{turnId}` | Status only. Added at M2 so a client whose socket is down can tell a running turn from a finished one — the "still working" state has to be truthful rather than hopeful |
| `GET` | `/api/sessions/{sid}/events/{seq}/attachments/{index}` | An attachment's bytes, for the transcript's image previews ([06-frontend.md](06-frontend.md)). Added at M2. Addressed by **position** rather than by artifact name or `gs://` URI: a session lives under the caller's uid, so reaching an event at all proves ownership and no caller-supplied storage path is ever validated |

#### `POST /api/sessions/{sid}/turns`

```jsonc
// request
{ "text": "here's my attempt at the exercise",
  "attachments": [ { "uploadId": "…", "mimeType": "image/png" } ],
  "confirmation": { "functionCallId": "…", "confirmed": true },   // M3; see below
  "idempotencyKey": "…" }

// 202 response — returns immediately, generation continues in background
{ "turnId": "t_01J…", "sessionId": "…", "status": "running", "startSeq": 0 }
```

The handler creates `turns/{turnId}`, spawns a detached `asyncio.Task`, and returns. It
does **not** await generation. Streaming is observed over the WebSocket.

`confirmation` was added at M3 and answers a tool that asked first. `discard_task`
"requires user confirmation" ([03-agent-design.md](03-agent-design.md)), which is
implemented with ADK's `require_confirmation`: the turn proposing it ends with an
`adk_request_confirmation` function call and the tool body runs only when a matching
function *response* arrives. That response is a turn like any other, carrying the id of
the call it answers — and **a turn carrying only a confirmation is valid**, since pressing
a button sends no text and no attachment.

#### `POST /api/sessions/{sid}/research`

```jsonc
// request
{ "reason": "I just added this task and want materials now",
  "budgetMinutesOverride": 90,       // optional
  "force": false,                    // re-run even if researchStatus == done
  "attachments": [ { "uploadId": "…", "mimeType": "application/pdf" } ] }  // + M8, optional

// 202
{ "runId": "r_01J…", "turnId": "t_01J…", "sessionId": "s_01J…", "mode": "inline" }
```

`sid` names the conversation the request came from — a task's own session, or, since M8,
the project's intake session — and is only ever used to resolve *what* to research and to
check ownership. The turn itself runs in a **new session created for this run**
(`sessionId` above), never in `sid`; see below and
[03-agent-design.md](03-agent-design.md#research_agent).

Behaviour:
- Resolves the linkage from `sid`. A task-linked session researches that task, subject to
  the same-task rules below. **Since M8, a session with no task (`taskId: null`) is
  accepted too** — the project's intake session — and researches `reason` as a
  free-standing question about the project rather than any one task. `reason` must be
  non-empty in that case: there is no task description to fall back on, and an empty
  request would have nothing to research. `force` and the "already has materials" check
  do not apply here — nothing before is a target to re-run; every taskless call opens a
  fresh question. The budget the model works to is the project's `defaultTaskMinutes`;
  `budgetMinutesOverride` is accepted and recorded on the run but is not threaded into the
  model's own budget for either a task-scoped or a taskless request — a pre-existing gap
  from M4, not one M8 opens or closes.
- Task-linked behaviour is unchanged from M4/M5: verifies the task is a leaf (a task with
  subtasks refuses — each subtask is researched on its own), and refuses with `409` if
  `researchStatus == "done"` and `force` was not set.
- Acquires the project agent lease. If the lease is held by an autonomous run, returns
  `409` with `{ runId }` of the in-flight run so the UI can attach to it instead of
  starting a duplicate.
- Creates `autonomous_runs/{runId}` with `trigger: "manual"`, `mode: "inline"`, `taskId`
  set or `null`, and a fresh `research` session
  ([02-data-model.md](02-data-model.md#sessions--events-adk-owned-layout)) — and runs the
  **same** `autonomous_workflow` steps against it, so manual and scheduled research cannot
  drift.
- Sets `task.researchStatus` to `in_progress` before the first model call and to `done` or
  `failed` at the end, which is what invariant 6
  ([02-data-model.md](02-data-model.md#task-state-machine)) reads to keep a task from
  completing itself while its checklist is still being written. Not applicable, and not
  touched, for a taskless run.
- Streams progress over the WebSocket as the turn identified by `turnId`, in the session
  identified by `sessionId` — a client watching this run subscribes to that session's
  transcript, not to `sid`'s.

**What M4 implements of this, and what waits for M5.** The endpoint, the lease, the ledger
document, and the `research` and `post_report` steps are M4 — they are the whole of the
manual path, and golden flow #5 exercises them. The trigger chain that *schedules* a run
(`/internal/tick`, Cloud Tasks, recovery, the presence guard) and the two steps that reshape
the board (`propose_tasks`, `reprioritize`) are M5. A manual run therefore has a `steps[]`
array with the two later steps absent rather than `pending`, so that "first non-complete
step" stays a truthful `cursor` and M5's executor does not inherit a backlog of runs it
thinks it left half-finished. Subscribing by `runId` also stays M5
([09-roadmap.md](09-roadmap.md#status-after-m2)): a manual run has a `turnId`, and the turn
is what the client watches.

### Runs

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/api/runs/{runId}` | Ledger status: `status`, `steps[]`, `cursor`, `taskId`, `turnId`. Backs the `['run', runId]` query ([06-frontend.md](06-frontend.md)) and the 409 attach path below |
| `POST` | `/api/runs/{runId}/undo` | Reverses the run's writes; idempotent, returns the affected `taskIds` |
| `GET` | `/api/projects/{id}/runs` | Recent runs for the project, newest first — backs the "Updated by your coach" banner and, since M8, the board's "latest research" card (the first entry whose steps include `research`) |
| `GET` | `/api/tasks/{id}/runs` | + M8. Recent runs for one task, newest first — backs the task workspace's research card the same way the project route backs the board's. A query rather than a denormalized pointer, on the same reasoning `list_for_project` already uses: the ledger is the authority, and there is no task-level cache of it to keep in step |

`POST /api/runs/{runId}/undo` is the endpoint behind the one-click undo in
[05-autonomous-runs.md](05-autonomous-runs.md#what-the-run-is-allowed-to-change) and golden
flow #6 ([08-testing.md](08-testing.md)). It deletes tasks the run created and restores the
`order` / `nextUpTaskId` values the ledger recorded, in one transaction. It is refused with
`409` while the run is still `running`, and is a no-op returning `200` if already undone —
so a double-click cannot half-reverse a run.

### Uploads

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/uploads` | `{ filename, mimeType, sizeBytes }` → `{ uploadId, signedUrl }` (V4 resumable PUT to GCS) |
| `POST` | `/api/uploads/{id}/finalize` | Server verifies size/type, scans, registers ADK artifact |

Accepted: `image/png`, `image/jpeg`, `image/webp`, `application/pdf`, `text/plain`,
`text/markdown`. Cap 20 MB. MIME sniffed server-side, not trusted from the client.

### Internal

| Method | Path | Caller |
| --- | --- | --- |
| `POST` | `/internal/tick` | Cloud Scheduler |
| `POST` | `/internal/runs/{runId}/execute` | Cloud Tasks |
| `GET` | `/livez`, `/readyz` | Cloud Run probes. **Not `/healthz`** — Google's frontend intercepts that path on Cloud Run and answers it with its own 404 without forwarding to the container, which is invisible to the probes (they bypass the frontend) and to every local test |

## WebSocket protocol (`/ws`)

One connection per browser tab, multiplexed across sessions. JSON frames, every frame has
`type`. Server→client frames carrying stream content also carry `turnId` and `seq`.

### Client → server

```jsonc
{ "type": "subscribe",   "turnId": "t_…" }
{ "type": "subscribe",   "runId":  "r_…" }                       // run_status frames only
{ "type": "resume",      "turnId": "t_…", "lastSeq": 42 }
{ "type": "unsubscribe", "turnId": "t_…" }
{ "type": "unsubscribe", "runId":  "r_…" }
{ "type": "presence",    "projectId": "p_…", "taskId": "k_…" }   // every 30 s
{ "type": "ping" }
```

`subscribe` takes exactly one of `turnId` or `runId`. Subscribing by `runId` is what makes
the `409` from `POST /api/sessions/{sid}/research` actionable: the client attaches to the
in-flight run's `run_status` frames instead of starting a duplicate. A scheduled run has no
`turnId` at all, so run subscription is the only way to watch one — which golden flow #6
depends on. Both are ownership-checked against the socket's principal at subscribe time.

### Server → client

```jsonc
{ "type": "turn_start",    "turnId": "…", "sessionId": "…" }
{ "type": "delta",         "turnId": "…", "seq": 43, "text": "…" }
{ "type": "tool_call",     "turnId": "…", "seq": 44, "name": "youtube_find_by_duration",
                           "argsPreview": {…} }        // rendered as a status chip
{ "type": "tool_result",   "turnId": "…", "seq": 45, "name": "…", "ok": true }
{ "type": "artifact",      "turnId": "…", "seq": 46, "kind": "research_report",
                           "reportId": "…", "taskId": "…" }
{ "type": "turn_complete", "turnId": "…", "seq": 47, "eventIds": ["…"] }
{ "type": "turn_error",    "turnId": "…", "seq": 47, "code": "…", "message": "…", "retryable": true }
{ "type": "board_update",  "projectId": "…", "taskIds": ["…"], "origin": "agent", "runId": "…" }
{ "type": "run_status",    "runId": "…", "step": "research", "status": "running" }
{ "type": "pong" }
```

`board_update` is the invalidation push that keeps the task board live while the
autonomous agent works — the client turns it into a TanStack Query invalidation rather
than trying to patch state from the message.

## Surviving client disconnects

The requirement: *generation must complete even if the client disconnects, so inference is
not wasted.* Mechanism:

1. **Ownership.** The generation coroutine is created with `asyncio.create_task` and held
   in an app-level `TurnRegistry`, not in the WebSocket handler's scope. Closing a socket
   drops a *subscriber*; nothing cancels the task. The only cancellation paths are the
   explicit cancel endpoint and process shutdown.
2. **Fan-out.** A `StreamBroker` maps `turnId → set[asyncio.Queue]`. Zero subscribers is a
   normal state: chunks still increment `seq` and still get checkpointed.
3. **Checkpointing.** Deltas accumulate in a buffer flushed to `turns/{turnId}.checkpoints`
   every 400 ms or 512 characters, whichever first. On completion, finalized ADK events go
   to `sessions/*/events` and `status` becomes `complete`.
4. **Resume.** On reconnect the client sends `resume` with its `lastSeq`. The server:
   - reads `turns/{turnId}`; if `complete`, replays checkpoints `> lastSeq`, then
     `turn_complete`, then the client refetches the finalized event;
   - if `running` **on this instance**, replays checkpoints `> lastSeq` and attaches to the
     live broker — no gap, because replay and attach happen under the broker lock;
   - if `running` **on another instance**, replays checkpoints and then follows the
     Firestore document with a snapshot listener until `complete`. Coarser granularity
     (400 ms chunks instead of token-level), still correct, still no wasted inference.
5. **Graceful shutdown.** On `SIGTERM` Cloud Run gives a termination grace period. The app
   stops accepting new turns, waits up to the grace period for in-flight turns, and marks
   any survivors `failed` with `retryable: true`. Manual/autonomous runs are picked up by
   the ledger sweep; interactive turns surface a retry affordance.
6. **Cloud Run settings that make this real** (see [07-infra-deploy.md](07-infra-deploy.md)):
   CPU always allocated (no request-based throttling — otherwise CPU is throttled to near
   zero once the response is "done"), `min-instances ≥ 1`, request timeout 3600 s,
   session affinity on, and per-instance concurrency tuned so background turns are not
   starved.

## Rate limits

| Scope | Limit |
| --- | --- |
| Turns | 30/min/user, 5 concurrent per user |
| Manual research | 10/hour/user |
| Uploads | 50/hour/user, 20 MB each |
| WebSocket | 3 connections/user, 100 frames/min |

Enforced in Firestore counters with a token-bucket; exceeding returns `429` with
`Retry-After`.
