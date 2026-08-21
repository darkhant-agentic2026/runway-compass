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
board_events/{uid}                           ← board_update across instances (M5)
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

  ```jsonc
  { "ownerUid": "…", "objectName": "{uid}/{uploadId}/{filename}",  // in the *staging* bucket
    "filename": "screenshot.png", "mimeType": "image/png", "sizeBytes": 20481,
    "status": "pending" | "ready",
    "artifactFilename": "user:{uploadId}",   // set by finalize; ADK-scoped to the user
    "artifactVersion": 0,
    "artifactUri": "gs://{project}-coach-artifacts/…",  // what a turn actually references
    "createdAt": ts, "finalizedAt": ts, "expiresAt": null }
  ```

`board_events/{uid}` was added at M5, and it is the same argument a third time: a
`board_update` has to reach the owner's tabs, and until M5 every board mutation came from
a tool call inside a turn the user's own request had started — so the in-process hub reached
all of them. A scheduled run executes wherever Cloud Tasks lands it, with no relationship to
where the owner is connected.

```jsonc
{ "rev": 42,
  "frames": [ { "rev": 41, "instanceId": "…", "frame": { /* board_update */ } }, … ] }
```

One document per user, appended transactionally and trimmed to the last 20, read by a poller
on every instance holding a socket for that user. A *document* rather than a subcollection
because this is read on a timer and the cheapest read available is the right one; **polled**
rather than watched for the same reason the cross-instance resume path polls (`on_snapshot`
exists only on the synchronous `DocumentReference`). Each frame carries the instance that
wrote it, so the writer's own poller skips what it has already delivered locally. Losing
intermediate frames to the trim costs nothing: every frame is the same instruction —
refetch — and the last one is enough.

  **`objectName` and `artifactUri` point at different buckets, and only the second is
  durable.** `{project}-coach-uploads` is staging and carries
  `lifecycle_rule { age = 1 → Delete }`; a GCS lifecycle rule cannot express
  "unfinalized", so it collects finalized objects just as happily. `finalize` therefore
  copies the verified bytes into `{project}-coach-artifacts` through `GcsArtifactService`
  ([03-agent-design.md](03-agent-design.md#artifacts)) and every later reference uses
  `artifactUri`. Referencing the staging object instead fails *a day later*, silently —
  a session's history is replayed to the model on every turn, so the symptom is a coach
  that has forgotten a screenshot it discussed yesterday.

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
    "confirmItemCompletion": true,      // ask before the coach ticks a step off

    "preferredSources": ["…"], "avoidSources": ["…"]
  },
  "nextUpTaskId": "…",                  // denormalized pointer, transactional
  "intakeSessionId": "…",               // the session POST /api/projects opens (M3)
  "counts": { "total": 12, "completed": 5, "openMinutes": 340 },
  "lastAutonomousRunAt": ts,
  "createdAt": ts, "updatedAt": ts
}
```

Preference resolution is a pure function, `resolve_prefs(global, project) -> EffectivePrefs`,
used identically by the API, the UI (via `GET /api/projects/{id}/effective-prefs`), and
the agent's prompt builder. The example from the brief — global default 45 min, project
override 2 h — is exactly this: `prefs.defaultTaskMinutes = 120`.

`intakeSessionId` was added at M3. `POST /api/projects` creates a session with
`taskId: null` and nothing pointed back at it, so every later visit to the project would
have had to find it by scanning the project's sessions. It is a denormalized pointer on
the same footing as `nextUpTaskId`: the collection-group scan above is still the
authority, and it repairs the pointer when it runs.

## `projects/{projectId}/tasks/{taskId}`

```jsonc
{
  "projectId": "…", "ownerUid": "…",     // denormalized for collection-group queries
  "parentTaskId": null | "…",
  "title": "…", "description": "…",
  "state": "draft",                      // see state machine below
  "postponedUntil": null | ts,           // set iff state == "postponed_until"
  "estimatedMinutes": 45,
  "actualMinutes": null,
  "order": "0|hzzzzz:",                  // fractional index (LexoRank-style)
  "sessionId": "…",                      // 1:1 with an ADK session, created lazily
  "needsResearch": true,
  "researchStatus": "none" | "pending" | "in_progress" | "done" | "failed",
  "researchRequestedAt": null | ts,      // set iff researchStatus == "pending"
  "latestReportId": null | "…",
  "items": [                             // LEAF tasks only; see Task items below
    { "itemId": "i_01J…", "shortDescription": "…", "details": "…",
      "guided": true, "completed": false, "completedAt": null,
      "sourceReportId": "rep_01J…" | null }
  ],
  "rollup": {                            // maintained on PARENT tasks only
    "subtaskCount": 4, "completedSubtasks": 1, "totalEstimatedMinutes": 150
  },
  "origin": "user" | "agent",
  "createdAt": ts, "updatedAt": ts, "completedAt": null
}
```

**`needsResearch`, `researchStatus`, and `researchRequestedAt` answer three different
questions**, and M5 is where the third one starts having a writer:

| Field | Question | Written by |
| --- | --- | --- |
| `needsResearch` | *Would* this task benefit from prepared material? | Whoever created the task — the coach's `add_task`/`add_subtask`, or the learner's form |
| `researchStatus` | Where is that material in its lifecycle? | The research path: `pending` when queued, `in_progress` while a run holds it, `done`/`failed` at the end |
| `researchRequestedAt` | Did the *learner* ask for it, and when? | The queue button alone ([04-api-contract.md](04-api-contract.md#tasks)) |

`researchRequestedAt` is a timestamp rather than a boolean because it is doing two jobs:
non-null *is* the priority flag, and its value is the fairness order among several queued
tasks. A boolean would need a second field to break ties, and would have to be kept
consistent with it.

The pair is what [05-autonomous-runs.md](05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them)
splits scheduled work on: `needsResearch` with `researchStatus ∈ {none, failed}` is work
the *coach* signed up for, and is skipped while the owner is present; `researchStatus ==
"pending"` with a `researchRequestedAt` is work the *learner* asked for, and runs first
regardless. The invariant between the last two is that neither exists without the other —
`pending` with no timestamp would be a request nothing can order, and a timestamp on any
other status is a request that already ran.

`researchStatus ∈ {pending, in_progress}` is also half of invariant 6: a task whose
checklist is fully ticked does **not** auto-complete while research is outstanding, because
more items are about to arrive. Queueing research on an already-`completed` task does not
reopen it, though — the derivation only reopens on an item being un-ticked.

`items` and `rollup` are the same field in two moods and are mutually exclusive by
construction: a leaf task's plan is its item list, a parent's plan is its subtasks. Nothing
enforces the exclusion with a validator, because creating a subtask is the only way a leaf
becomes a parent, and that write *moves* the items onto the first child rather than dropping
them — the learner would otherwise lose a checklist, possibly a half-finished one, to a
reshape they did not ask for.

`POST /api/tasks/{id}/split` and the `split_task` tool did this differently and were removed
after M4: they created a whole breakdown in one call and simply refused a task that had
items, because there was nowhere to put them. One child at a time has somewhere.

### Task items

A leaf task carries an **ordered, unnumbered** list of the things that have to happen for it
to be done. Ordered because the coach works through them in sequence and the reading has to
come before the exercise that uses it; unnumbered because the count is not a promise — a
re-run of research replaces the list, and "step 3 of 7" that becomes "step 3 of 5" reads as
work disappearing rather than as a better plan.

| Field | Meaning |
| --- | --- |
| `itemId` | Stable, server-assigned. Completion and the tool's argument key on it |
| `shortDescription` | The one-liner the checklist renders. Not a title — a thing to do |
| `minutes` | What this item is expected to cost, carried over from the report item. `null` on a hand-added item; the budget meter sums what it has |
| `url` | The thing an unguided item points at, if there is one. Also half of the identity a re-run matches on |
| `details` | What the coach teaches *from*. For an unguided item this is the instruction itself ("read §3–4 of this page", with the link); for a guided one it is the material the coach needs in order to walk the learner through it, and the learner never sees it verbatim |
| `guided` | Whether the coach walks the learner through this in the session, or the learner goes and does it and comes back to say so |
| `completed`, `completedAt` | User-owned. Written by the checkbox and by `complete_task_item`, never by free-form model output |
| `sourceReportId` | The report that contributed the item, or `null` for one the learner added by hand |

**`guided` is a routing decision, not a difficulty rating.** An unguided item is one whose
work happens outside the conversation — watch this video, read that page, run this tutorial
— so the coach's job is to hand it over and then wait. A guided item is one the coach does
*with* the learner: the Socratic walkthrough, the exercise it marks, the concept it explains
against the learner's own code. The distinction is what stops the coach from narrating a
YouTube video it cannot see, and from silently skipping the teaching it is actually for.

**The list is populated by the research agent** and replaced wholesale when research re-runs:
`post_research_report` writes the report and promotes its `required[]` entries into
`items[]` in the same transaction ([03-agent-design.md](03-agent-design.md#domain-tools)).
Completion state survives the replacement where an item's `shortDescription` and `url` are
unchanged, so a re-run that keeps a reading the learner has already done does not ask them
to do it again.

**Items move between a task and its subtasks with their ids and their ticks intact**
(`TaskService.move_items`, reached by the agent as `move_task_items`). That is what makes
redistributing a checklist after a breakdown a rearrangement rather than a rewrite: a
re-added item is a *new* item, and loses both what the learner had finished and the id a
report's `progress.feedback` points at. Both tasks are written in one transaction, because
the derived state of each moves — a source whose last outstanding step leaves is finished,
and a `draft` destination that gains its first is no longer plan-less. The learner may also add, edit, reorder, and delete items by hand; a
hand-added item has `sourceReportId: null` and is never dropped by a re-run.

**A leaf task with every item completed and no research outstanding becomes `completed`.**
The check runs in the same transaction as the item write, and its two halves are both
necessary: items alone would complete a task the moment the first thin report's list was
ticked off, while research was still running and about to add five more items to it. So the
rule is `all(items.completed) and items != [] and researchStatus not in {pending,
in_progress}`. A leaf with no items at all never auto-completes — an empty list is the
absence of a plan, not a finished one — and a parent never does: its plan is its subtasks,
and invariant 4 already makes completing one with unfinished children a decision the UI puts
to the learner.

### Task state machine

```
   ┌──────────────┐  plan appears
   │    draft     │──(items or subtasks created)──┐
   └──────┬───────┘                               │
          │                                       ▼
          │                         ┌──────────────┐
          │            ┌───────────▶│ not_started  │◀─────────────────────┐
          │            │            └──────┬───────┘                      │
          │            │          start    │                              │ un-postpone /
          │  start     │                   ▼                              │ restore
          │            │            ┌──────────────┐                      │ (manual, or the
          └───────────────────────▶ │ in_progress  │                      │  nightly sweep
                       │            └──┬────────┬──┘                      │  once postponed-
                       │      complete │        │ defer                   │  Until is past)
                       │               │        ├──────────▶┌──────────────┴─┐
                       │   reopen      │        │           │   postponed    │
                       │               │        │           └────────────────┘
                       │               │        │ defer until…              ▲
                       │               │        └──────────▶┌───────────────┴┐
                       │        ┌──────▼───────┐            │postponed_until │ (+ ts)
                       └────────│  completed   │            └────────────────┘
                                └──────────────┘
                                             ┌────────────────┐
   any state ──────────discard─────────────▶ │   discarded    │
                                             └────────────────┘
```

`draft` is where every task starts, whoever created it. It means "on the board, no plan
yet": a title and an estimate, and nothing said about how the work actually gets done. A
task leaves `draft` for `not_started` the moment it acquires a plan — its first items, or
its first subtasks — which for an agent-created task is usually the research run that
follows it onto the board, and for a hand-written one is whenever the learner or the coach
gets round to it.

**`draft` is not a lock.** The learner may start a draft task directly
(`draft → in_progress`) or discard it, exactly as they could a `not_started` one; `draft`
carries every transition `not_started` has, plus the automatic one. A board that refused to
let someone begin work until an LLM had blessed it with a checklist would be a worse board
than the one that existed before M4. What `draft` buys is a truthful "next up" and a visible
target for research — not a gate.

`postponed` and `postponed_until` are distinct states, not one state with an optional
field: the first waits for the user, the second waits for a clock. Both return to
`not_started` — the first only by user action, the second also by the sweep in
`/internal/tick`.

**`in_progress` replaced `current` at M4, and the rename carried a rule with it.** `current`
was singular by construction — one current task per project, the previous one demoted on
every start — and that was a claim about the learner's attention that the data could not
actually keep: research runs while a task sits open, a parent and a subtask are worked in
the same sitting, and demoting one to start another lost the fact that the first was
half-done. `in_progress` describes the task, not the learner's focus, so **any number of
tasks may be `in_progress` at once** and starting one demotes nothing. What the board pins
as "Next up" is `project.nextUpTaskId`, which is now derived rather than enforced: the
lowest-`order` top-level task that is `in_progress`, or failing that the lowest-`order` one
available to start.

Invariants enforced in a Firestore transaction by `TaskService`:

1. **A task's state and its plan agree.** A `draft` task that gains its first item or its
   first subtask becomes `not_started` in the same transaction. This is the only automatic
   promotion out of `draft`; losing every item again does not send it back, because by then
   the learner has seen a plan and a task silently regressing is worse than a stale state.
2. `postponed_until` requires a future `postponedUntil`; a nightly sweep (part of
   `/internal/tick`) flips expired ones back to `not_started`.
3. `discarded` is terminal except by explicit user restore. The agent may *propose*
   discarding but the `discard_task` tool is gated to user confirmation.
4. Completing a task with incomplete subtasks is allowed but surfaces a confirmation in
   the UI ("3 subtasks not done — complete anyway?").
5. Any subtask write recomputes the parent's `rollup` in the same transaction. Parent
   cards therefore render counts and summed minutes with no extra reads — which is the
   "card shows number of sub-tasks and total estimated duration" requirement.
6. **A leaf task whose items are all complete, with no research outstanding, is
   `completed`** — evaluated on every item write, in that write's transaction, and never on
   a parent. See [Task items](#task-items) for why both halves of the condition are load-bearing.

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
subtask is still too big, the coach adds more, smaller siblings instead. This keeps rollups, ordering, and UI
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
    "feedback": { "i_01J…": "down" }   // thumbs-down feeds the learner profile (R5)
  },
  "createdAt": ts, "updatedAt": ts
}
```

`itemId` is assigned server-side by `post_research_report`, not by the model — the model
returns items, the tool numbers them. Without stable ids, per-item feedback breaks the
moment a report is re-run and item order shifts, and the promotion into `task.items` would
have nothing to key on.

**Completion lives on the task, not on the report.** `post_research_report` promotes every
`required[]` entry into `tasks/{taskId}.items[]` in the same transaction that writes the
report, carrying the `itemId` across; the checkbox the learner ticks writes there. This is a
change from the pre-M4 design, which kept `progress.completedItemIds` on the report — two
reports for one task meant two checklists, and neither of them was the answer to "what is
left to do on this task". `progress` survives holding only `feedback`, which genuinely does
belong to the report: a thumbs-down is a judgement about *this recommendation*, and it has
to stay attached to the recommendation when a re-run supersedes it.

`progress` is a separate sub-object because it has a different writer and a different
lifetime from the report body: the report is immutable once written, feedback accumulates
against it.

`required` vs `optional` being separate arrays — rather than a `required: bool` flag on a
flat list — makes it structurally impossible for the UI to blur the distinction, and makes
`totalRequiredMinutes ≤ budgetMinutes` a validatable invariant. The tool rejects a report
that overruns the budget and asks the model to move items to `optional`. Only `required[]`
is promoted; an optional item is material the learner may want and is not a thing they owe
the task, which is exactly the distinction a checkbox would erase.

`kind` survives the promotion as far as the item's `guided` flag: an `exercise` or a
`code_scaffold` is guided, an `article`, `video`, or `doc` is not. The model may override
per item — some readings genuinely want walking through — but the default falls out of the
kind, so a report that says nothing about guidance still produces a sensible checklist.

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
- **Everything inside `event_data` is `snake_case`** — `file_data`, `mime_type`,
  `function_call`, `display_name`. This surprises, because ADK's `Event` declares
  `alias_generator=to_camel`, so its *aliases* are camelCase and every other camelCase
  field in this database invites the same assumption. But `append_event` stores
  `event.model_dump(exclude_none=True, mode="json")`, and `model_dump` defaults to
  `by_alias=False`.

  A reader that assumes the aliases finds nothing and reports *absence* rather than
  failing — an attachment that silently stops existing, not an error. `apps/web` reads this
  shape in `lib/transcript.ts` and is tested against `session-event-vectors.json`,
  generated from real dumps by `scripts/gen_event_vectors.py`
  ([08-testing.md](08-testing.md#end-to-end-playwright)); regenerate it after an ADK bump
  rather than hand-writing a fixture.

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
| Task's reports, newest first | `research_reports`: `taskId ASC, createdAt DESC` — two fields, so a composite. The emulator answers it without one; Firestore returns `FAILED_PRECONDITION` on the first deployed call, which is the first row of [09-roadmap.md](09-roadmap.md#what-a-green-local-run-does-not-prove) and the reason this row was written in the same change as the query |
| Expired postponements sweep | collection group `tasks`: `state ASC, postponedUntil ASC` |
| Requested research queue | collection group `tasks`: `researchStatus ASC, researchRequestedAt ASC` — the tick's first query, across every owner. Two fields on a collection group, so a composite that the emulator will answer without and Firestore will not ([09-roadmap.md](09-roadmap.md#what-a-green-local-run-does-not-prove), row 1). The task state is filtered in Python, for the same reason `sessions.projectId` is: a third field would be a wider index for a list already bounded by how many requests can be outstanding |
| Projects list | `projects`: `ownerUid ASC, status ASC, updatedAt DESC` |
| Autonomous candidates | `projects`: `status ASC, lastAutonomousRunAt ASC` |
| Stuck runs | `autonomous_runs`: `status ASC, leaseExpiresAt ASC` |
| Session events | `events`: `seq ASC` — single-field, ascending; the collection is nested under one session, so no composite is needed |
| Memory search | `memories`: `keywords ARRAY_CONTAINS_ANY, createdAt DESC` |
| Session by task | `sessions` collection group: `taskId ASC` — resolves a task to its session without storing the reverse pointer twice |
| Task by bare id | collection group `tasks`: `id ASC` — `GET /api/tasks/{id}` and friends address a task without its project (see below) |
| Session by project | `sessions` collection group: `projectId ASC` — the fallback in `get_or_create_intake`, for a project created before `intakeSessionId` existed. **One filter**: `taskId is None` is applied in Python, because a second `where` makes this a composite collection-group query that the emulator answers and Firestore refuses |

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
