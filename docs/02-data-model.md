# Data Model (Firestore)

Native-mode Firestore, single database, region co-located with Cloud Run (`us-central1`).
Access is IAM-only — all reads and writes go through the API service account. See
[Access model](#access-model) below.

## Collection map

Two groups of collections live here: the ones this project designs, and the ones whose
layout is **dictated by the ADK classes we subclass** ([03-agent-design.md](03-agent-design.md)).

App-owned:

```
users/{uid}
users/{uid}/memories/{memoryId}              ← CoachMemoryService
projects/{projectId}
projects/{projectId}/tasks/{taskId}
projects/{projectId}/research_reports/{reportId}
projects/{projectId}/locks/agent             ← single doc, lease
turns/{turnId}                               ← streaming checkpoints
turns/{turnId}/checkpoint_pages/{page}       ← spill when the turn doc nears 1 MiB
autonomous_runs/{runId}                      ← durable job ledger
presence/{uid}
usage/{uid}_{yyyymmdd}                       ← token + run counters
idempotency/{uid}__{fingerprint}             ← Idempotency-Key replay records, TTL 24 h
ws_tickets/{ticket}                          ← single-use socket tickets, TTL 60 s
uploads/{uploadId}                           ← what a signed upload URL was issued for
```

`idempotency/*` was added at M1 to make the `Idempotency-Key` contract in
[04-api-contract.md](04-api-contract.md) real across instances: the first request stores
its response body and status, a replay returns them without re-executing the handler. The
fingerprint hashes method, path, and key, so the same key reused on a different endpoint
is a different operation rather than an unrelated replay. Scoped by `uid` so one user's
key cannot collide with another's.

`ws_tickets/*` and `uploads/*` were added at M2 on the same footing — the API contract
needs cross-instance state that no existing collection holds:

- **`ws_tickets/{ticket}`** backs `POST /api/ws-ticket`. It is a *collection* rather than
  a process-local dict because session affinity is a preference, not a guarantee, and it
  is least reliable exactly when it matters — during a redeploy or a scale event, which
  is also when clients are most likely to be reconnecting. An in-process store would fail
  those reconnects with what looks like an authentication bug. "Redeemed and deleted" is
  one transaction, so two sockets racing on one ticket cannot both be admitted.
- **`uploads/{uploadId}`** records the object name, owner, and declared type behind a
  signed URL. The browser PUTs straight to GCS, so the server is out of the data path and
  needs somewhere to remember what an upload id refers to and who may reference it.
  Scoped by `ownerUid`, checked on every read.

ADK-owned — this layout comes from the shipped `FirestoreSessionService` and is **not ours
to choose**; changing it means not subclassing:

```
adk-session/{appName}/users/{uid}/sessions/{sessionId}
adk-session/{appName}/users/{uid}/sessions/{sessionId}/events/{eventId}
app_states/{appName}                         ← ADK `app:` scoped state
user_states/{appName}/users/{uid}            ← ADK `user:` scoped state
```

The root collection name (`adk-session`) is the constructor's `root_collection` argument,
defaulting to the `ADK_FIRESTORE_ROOT_COLLECTION` env var. Pin it explicitly in
`Settings` rather than relying on the default.

Tasks are a subcollection of the project (never global) so that a project's board is one
collection query and one security boundary.

## `users/{uid}`

```jsonc
{
  "email": "…", "displayName": "…", "photoUrl": "…",
  "createdAt": ts, "lastSeenAt": ts,
  "globalPrefs": {
    "defaultTaskMinutes": 45,
    "guidanceStyle": "socratic",        // socratic | direct | mixed
    "verbosity": "balanced",            // terse | balanced | thorough
    "timezone": "Europe/Berlin",
    "autonomousEnabled": true,
    "autonomousQuietHours": { "start": "23:00", "end": "07:00" }
  },
  "learnerProfile": {                   // agent-maintained, user-editable
    "thinkingStyle": "…",               // free text, ≤ 500 chars
    "strengths": ["…"], "gaps": ["…"],
    "technologies": [{ "name": "python", "level": "intermediate", "evidence": "…" }],
    "pacing": "…",
    "feedbackNotes": ["…"],             // capped ring buffer, 20 entries
    "updatedAt": ts, "updatedBy": "agent" | "user", "version": 7
  },
  "plan": { "tier": "free", "limits": { "autonomousRunsPerDay": 20 } }  // billing hook
}
```

`learnerProfile` is written **only** by the `update_learner_profile` tool with a Pydantic
schema and by the Settings UI. Never by free-form model output. Every write bumps
`version` and appends to an audit trail so the user can see why the coach changed its
approach.

## `projects/{projectId}`

```jsonc
{
  "ownerUid": "…",
  "title": "…", "goal": "…",            // goal refined through Socratic intake
  "status": "active" | "paused" | "archived",
  "prefs": {                            // null field = inherit from globalPrefs
    "defaultTaskMinutes": 120,
    "guidanceStyle": null,
    "researchDepth": "standard",        // light | standard | deep
    "allowVideos": true,
    "preferredSources": ["…"], "avoidSources": ["…"]
  },
  "nextUpTaskId": "…",                  // denormalized pointer, transactional
  "counts": { "total": 12, "completed": 5, "openMinutes": 340 },
  "lastAutonomousRunAt": ts,
  "createdAt": ts, "updatedAt": ts
}
```

Preference resolution is a pure function, `resolve_prefs(global, project) -> EffectivePrefs`,
used identically by the API, the UI (via `GET /api/projects/{id}/effective-prefs`), and
the agent's prompt builder. The example from the brief — global default 45 min, project
override 2 h — is exactly this: `prefs.defaultTaskMinutes = 120`.

## `projects/{projectId}/tasks/{taskId}`

```jsonc
{
  "projectId": "…", "ownerUid": "…",     // denormalized for collection-group queries
  "parentTaskId": null | "…",
  "title": "…", "description": "…",
  "state": "not_started",                // see state machine below
  "postponedUntil": null | ts,           // set iff state == "postponed_until"
  "estimatedMinutes": 45,
  "actualMinutes": null,
  "order": "0|hzzzzz:",                  // fractional index (LexoRank-style)
  "sessionId": "…",                      // 1:1 with an ADK session, created lazily
  "needsResearch": true,
  "researchStatus": "none" | "pending" | "in_progress" | "done" | "failed",
  "latestReportId": null | "…",
  "rollup": {                            // maintained on PARENT tasks only
    "subtaskCount": 4, "completedSubtasks": 1, "totalEstimatedMinutes": 150
  },
  "origin": "user" | "agent",
  "createdAt": ts, "updatedAt": ts, "completedAt": null
}
```

### Task state machine

```
                     ┌──────────────┐
      ┌─────────────▶│ not_started  │◀─────────────────────┐
      │              └──────┬───────┘                      │
      │            start    │                              │ un-postpone / restore
      │                     ▼                              │ (manual, or the nightly
      │              ┌──────────────┐                      │  sweep once postponedUntil
      │              │   current    │                      │  is in the past)
      │              └──┬────────┬──┘                      │
      │        complete │        │ defer                   │
      │                 │        ├──────────▶┌──────────────┴─┐
      │   reopen        │        │           │   postponed    │
      │                 │        │           └────────────────┘
      │                 │        │ defer until…              ▲
      │                 │        └──────────▶┌───────────────┴┐
      │                 │                    │postponed_until │ (+ postponedUntil ts)
      │          ┌──────▼───────┐            └────────────────┘
      └──────────│  completed   │
                 └──────────────┘            ┌────────────────┐
   any state ──────────discard─────────────▶ │   discarded    │
                                             └────────────────┘
```

`postponed` and `postponed_until` are distinct states, not one state with an optional
field: the first waits for the user, the second waits for a clock. Both return to
`not_started` — the first only by user action, the second also by the sweep in
`/internal/tick`.

Invariants enforced in a Firestore transaction by `TaskService`:

1. **At most one `current` task per project.** Setting a task `current` demotes the
   previous one to `not_started`. `project.nextUpTaskId` is updated in the same
   transaction.
2. `postponed_until` requires a future `postponedUntil`; a nightly sweep (part of
   `/internal/tick`) flips expired ones back to `not_started`.
3. `discarded` is terminal except by explicit user restore. The agent may *propose*
   discarding but the `discard_task` tool is gated to user confirmation.
4. Completing a task with incomplete subtasks is allowed but surfaces a confirmation in
   the UI ("3 subtasks not done — complete anyway?").
5. Any subtask write recomputes the parent's `rollup` in the same transaction. Parent
   cards therefore render counts and summed minutes with no extra reads — which is the
   "card shows number of sub-tasks and total estimated duration" requirement.

The task document also carries its own `id` as a field, mirroring the document key. This
is the same denormalization-for-collection-group-queries the `projectId` and `ownerUid`
comments describe, and it exists because `/api/tasks/{id}` addresses a task without naming
its project: a collection-group query cannot filter on a document key by its trailing
segment, so there has to be a field to filter on. Ownership is then checked against the
task's own `ownerUid`, which keeps that lookup to one query.

Because invariant 5 makes every task write touch the parent's `rollup` and the project's
`counts`, two concurrent writes to one project *always* contend on the same one or two
documents. Firestore resolves that by aborting and retrying, which is correct but has a
budget: under enough concurrency the budget runs out and a perfectly valid write surfaces
as a 500.

`TaskService` therefore holds a **per-project `asyncio.Lock`**, so writes to one project
from one instance queue instead of colliding. This is the same move ADK's shipped
`FirestoreSessionService` makes for the identical problem
([03-agent-design.md](03-agent-design.md)), and it is an optimization rather than the
guarantee: a second Cloud Run instance has its own locks, so cross-instance safety still
rests entirely on the transaction, plus jittered backoff around it in
`repositories/firestore.py`. Both layers are tested — the same-instance path for being
deterministic, the cross-instance path by driving two service instances at once.

Task nesting is **one level deep**. A subtask cannot have subtasks; if the agent thinks a
subtask is still too big, it splits it into siblings. This keeps rollups, ordering, and UI
simple, and matches how bite-sized work actually decomposes.

### Ordering

`order` is a fractional index string. Inserting between neighbours computes a midpoint
key, so "make this the next-up task" is a single-document write, not a renumbering of the
board. Subtasks are ordered within their parent using the same scheme. On the rare key
collision or exhaustion, `TaskService` rebalances the whole project in one batch.

## `projects/{projectId}/research_reports/{reportId}`

```jsonc
{
  "taskId": "…", "runId": "…", "sessionId": "…",
  "summary": "…",
  "required": [                        // counts toward task completion
    { "itemId": "i_01J…",              // stable; per-item completion and feedback key on it
      "kind": "article" | "video" | "exercise" | "doc" | "code_scaffold",
      "title": "…", "url": "…", "minutes": 12,
      "why": "…",                      // why this is needed for THIS task
      "source": "youtube" | "web" | "generated",
      "meta": { "channel": "…", "publishedAt": "…", "durationIso": "PT12M3S" } }
  ],
  "optional": [ /* same shape — explicitly NOT required */ ],
  "totalRequiredMinutes": 38,
  "budgetMinutes": 45,
  "citations": [ { "uri": "…", "title": "…" } ],   // grounding metadata
  "progress": {                        // user-owned, never written by the agent
    "completedItemIds": ["i_01J…"],    // required items only; optional items have no checkbox
    "feedback": { "i_01J…": "down" }   // thumbs-down feeds the learner profile (R5)
  },
  "createdAt": ts, "updatedAt": ts
}
```

`itemId` is assigned server-side by `post_research_report`, not by the model — the model
returns items, the tool numbers them. Without stable ids, per-item completion breaks the
moment a report is re-run and item order shifts.

`progress` is a separate sub-object because it has a different writer and a different
lifetime from the report body: the report is immutable once written, progress accumulates
against it.

`required` vs `optional` being separate arrays — rather than a `required: bool` flag on a
flat list — makes it structurally impossible for the UI to blur the distinction, and makes
`totalRequiredMinutes ≤ budgetMinutes` a validatable invariant. The tool rejects a report
that overruns the budget and asks the model to move items to `optional`.

## Sessions + events (ADK-owned layout)

Written by `CoachSessionService`, which subclasses ADK's shipped `FirestoreSessionService`
(see [03-agent-design.md](03-agent-design.md)). Fields marked **+** are what the subclass
adds; everything else is the shipped shape and must not be renamed.

```jsonc
// adk-session/{appName}/users/{uid}/sessions/{sessionId}
{ "id": "…", "appName": "coach", "userId": "…",
  "state": "{\"…\":\"…\"}",               // JSON-encoded STRING, not a map
  "createTime": ts, "updateTime": ts,
  "revision": 42,                         // optimistic-concurrency counter; +1 per appended event
  "status": "DELETING",                   // present only mid-delete
  "projectId": "…", "taskId": null | "…"  // + linkage; taskId is null for a project intake session
}

// …/sessions/{sessionId}/events/{eventId}
{ "event_data": { /* full serialized ADK Event: invocationId, author, content,
                     actions.stateDelta, actions.artifactDelta, partial, turnComplete */ },
  "timestamp": ts,                        // SERVER_TIMESTAMP, set by ADK
  "appName": "…", "userId": "…",
  "seq": 42                               // + gap-free per-session sequence, == the revision
}                                         //   assigned by the appending transaction
```

Two things to note, both consequences of subclassing rather than hand-rolling:

- **`state` is a JSON string, not a Firestore map.** The shipped service `json.dumps`es it.
  Session state therefore cannot be queried or indexed — which is fine, because domain data
  is the queryable plane and session state is conversation scratch space.
- **The whole `Event` is nested under `event_data`.** Queries and indexes address `seq` and
  `timestamp` at the top level; anything inside `event_data` is read-back-only.

Only finalized events are persisted — `append_event` returns early on `event.partial`.

ADK `user:`-prefixed state lives on `user_states/{appName}/users/{uid}` and `app:` state on
`app_states/{appName}`. `get_user_state()` reads the former directly so cross-session facts
are available without loading a session; the shipped class does not implement it, so the
subclass does ([03-agent-design.md](03-agent-design.md#what-the-subclass-adds)).

## `turns/{turnId}`

```jsonc
{ "sessionId": "…", "ownerUid": "…",
  "status": "running" | "complete" | "failed" | "cancelled",
  "startedAt": ts, "endedAt": ts, "lastSeq": 128,
  "instanceId": "…",                      // which Cloud Run instance owns it
  "leaseExpiresAt": ts,
  "cancelRequested": false,               // + set by the cancel endpoint on any instance
  "checkpoints": [ { "fromSeq": 0, "toSeq": 40, "text": "…",
                     "lengths": [3, 5, …] } ],                   // ≤ 400 KB total
  "error": null }
```

Checkpoints are appended in slices so a resume can replay from any `lastSeq`. When the doc
approaches the 1 MiB limit, older slices spill to
`turns/{turnId}/checkpoint_pages/{page}`.

Two fields marked **+** were added at M2, both because the resume path needs them:

- **`lengths`** is the character count of each delta in the slice, in order, so
  `sum(lengths) == len(text)` and `len(lengths) == toSeq - fromSeq + 1`. A slice merges
  every delta between two flushes, so a client whose `lastSeq` falls *inside* one cannot
  be served by `fromSeq`/`toSeq` alone: replaying the slice whole would resend text it
  already rendered, and skipping it would lose the tail. `lengths` is what lets the replay
  cut the string at the right offset, which is what makes "no duplicates and no gaps" true
  at token granularity rather than approximately.
- **`cancelRequested`** is the instruction channel for a cancel served by an instance that
  does not own the turn. The owner polls it; the local `TurnRegistry` handles the case
  where the owner is the one being asked.

## `presence/{uid}`

```jsonc
{ "activeProjectId": "…", "activeTaskId": "…", "lastHeartbeatAt": ts, "connections": 2 }
```

Heartbeat every 30 s over the WebSocket. "Owner is working here" =
`activeProjectId == projectId && now - lastHeartbeatAt < 120s`.

## `autonomous_runs/{runId}`

See [05-autonomous-runs.md](05-autonomous-runs.md) for the full schema and semantics.

## Indexes

| Query | Index |
| --- | --- |
| Board: tasks by order, excluding hidden states | `tasks`: `state ASC, order ASC` |
| Next-up selection | `tasks`: `state ASC, needsResearch ASC, order ASC` |
| Expired postponements sweep | collection group `tasks`: `state ASC, postponedUntil ASC` |
| Projects list | `projects`: `ownerUid ASC, status ASC, updatedAt DESC` |
| Autonomous candidates | `projects`: `status ASC, lastAutonomousRunAt ASC` |
| Stuck runs | `autonomous_runs`: `status ASC, leaseExpiresAt ASC` |
| Session events | `events`: `seq ASC` — single-field, ascending; the collection is nested under one session, so no composite is needed |
| Memory search | `memories`: `keywords ARRAY_CONTAINS_ANY, createdAt DESC` |
| Session by task | `sessions` collection group: `taskId ASC` — resolves a task to its session without storing the reverse pointer twice |
| Task by bare id | collection group `tasks`: `id ASC` — `GET /api/tasks/{id}` and friends address a task without its project (see below) |

## Access model

The Firestore API accepts only IAM-authenticated callers, and the sole principal holding
`roles/datastore.user` is `coach-api-sa` ([07-infra-deploy.md](07-infra-deploy.md)). No
client-side SDK path exists, so a browser cannot reach the database at all. There is no
security-rules file, and none is needed: rules govern client-SDK access, which is not a
surface this deployment has.

Per-user isolation is therefore enforced entirely server-side. Every `services/` entry point
takes a `Principal` and filters on `ownerUid` before touching `repositories/`. That check is
the security boundary and is covered by tests.

Enabling direct-client realtime reads later would mean authoring rules from scratch — a
scoped, reviewed change.

## Retention

- `turns/*` — TTL 7 days (Firestore TTL policy on `endedAt`). Finalized content already
  lives in the session's `events` subcollection. Note that a Firestore TTL only fires on
  documents where the field is actually set, so a turn that never reaches a terminal state
  never expires: the SIGTERM drain and the ledger sweep both set `endedAt` when they mark a
  turn `failed`, and that is what keeps this from leaking.
- `autonomous_runs/*` — TTL 30 days on `updatedAt`, which every step boundary touches.
- `sessions`, `tasks`, `projects`, `memories` — retained; deleted on account deletion via
  a `DELETE /api/me` cascade job.
