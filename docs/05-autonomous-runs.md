# Autonomous Updates

The requirement, restated as invariants:

1. Interrupted work is finished before new work is started.
2. Work happens on the next-up, non-completed, non-discarded task.
3. Research results are posted into that task's session.
4. The agent may add tasks and reorder them as a result.
5. It never runs on a project whose owner is currently working in that project.

## Trigger chain

```
Cloud Scheduler (*/15 min, OIDC)
        │
        ▼
POST /internal/tick                      ← cheap, bounded, ≤ 30 s
        ├─ 1. sweep expired postponed_until tasks → not_started
        ├─ 2. RECOVERY: find runs {status: running, leaseExpiresAt < now}
        │            or {status: failed, attempts < maxAttempts}
        │            → re-enqueue each  (invariant 1)
        └─ 3. SCHEDULING: query candidate projects, apply guards,
                          create runs, enqueue to Cloud Tasks
        │
        ▼
Cloud Tasks queue `autonomous-runs`   (rate-limited, retried with backoff)
        │
        ▼
POST /internal/runs/{runId}/execute       ← does the actual work, up to 15 min
```

`/internal/tick` deliberately does no agent work. It is a fast, idempotent planner. All
LLM work happens in Cloud Tasks deliveries, which gives per-job retry, backoff, dispatch
rate limiting, and dedup for free, and keeps any single failure from poisoning the tick.

## Candidate selection and guards

A project is a candidate if all hold:

| Guard | Check |
| --- | --- |
| Owner enabled autonomy | `user.globalPrefs.autonomousEnabled` |
| Not quiet hours | Current time in the user's timezone outside `autonomousQuietHours` |
| Project active | `project.status == "active"` |
| Cooldown | `now - project.lastAutonomousRunAt > 6 h` |
| **Owner not present** | `presence/{ownerUid}` does not show `activeProjectId == projectId` with a heartbeat in the last 120 s |
| No lease held | `projects/{id}/locks/agent` absent or expired |
| Under quota | `usage/{uid}_{today}.autonomousRuns < plan.limits.autonomousRunsPerDay` |
| Work exists | At least one task in `draft`/`not_started`/`in_progress` with `researchStatus != done` |

`draft` is first in that list on purpose. From M4 it is the state a task starts in and the
one it leaves when research gives it a checklist ([02-data-model.md](02-data-model.md#task-state-machine)),
so a project full of drafts is exactly the project with the most for a run to do — and a
guard written before `draft` existed would have skipped it as having no work.

Ordering of candidates: `lastAutonomousRunAt ASC` (fairness), capped at N projects per
tick to bound cost.

### Presence guard details

Presence is written from the WebSocket: every `presence` frame updates
`presence/{uid}.{activeProjectId, activeTaskId, lastHeartbeatAt}`; disconnect decrements
`connections` and, at zero, clears `activeProjectId`. The guard is checked **twice** —
once at scheduling time and again inside the lease transaction at execution time —
because a Cloud Tasks delivery can arrive minutes after scheduling and the user may have
sat down in the meantime. If the second check fails, the run is abandoned with
`status: "skipped_owner_present"`, not failed.

## The lease

`projects/{projectId}/locks/agent`:

```jsonc
{ "holder": "run:r_01J…", "acquiredAt": ts, "expiresAt": ts, "instanceId": "…" }
```

Acquired in a Firestore transaction (create-if-absent-or-expired), renewed every 60 s by
the executing task with a 5-minute TTL, released in a `finally`. A crashed instance's
lease simply expires. The same lease is taken by manual research
(`POST /api/sessions/{sid}/research`), which is what makes "manual and autonomous never
collide" true rather than hopeful.

## Run ledger

`autonomous_runs/{runId}`:

```jsonc
{
  "ownerUid": "…", "projectId": "…", "taskId": "…",
  "trigger": "scheduled" | "manual",
  "mode": "queued" | "inline",
  "status": "pending" | "running" | "complete" | "failed" | "skipped_owner_present" | "cancelled",
  "attempts": 1, "maxAttempts": 3,
  "leaseExpiresAt": ts, "instanceId": "…",
  "steps": [
    { "id": "select_next_task", "status": "complete", "startedAt": ts, "endedAt": ts,
      "output": { "taskId": "k_…", "budgetMinutes": 45 } },
    { "id": "research",   "status": "running", "output": null, "error": null },
    { "id": "post_report","status": "pending" },
    { "id": "propose_tasks", "status": "pending" },
    { "id": "reprioritize",  "status": "pending" }
  ],
  "cursor": "research",                 // first non-complete step
  "usage": { "inputTokens": 0, "outputTokens": 0, "toolCalls": 0 },
  "turnId": "t_…",                      // present when mode == "inline"
  "createdAt": ts, "updatedAt": ts, "error": null
}
```

### Execution semantics

- The executor loads the run, resumes at `cursor`, and runs steps in order.
- **Each step commits its own output to the ledger before the next begins.** A crash
  during `propose_tasks` therefore never repeats `research` — the expensive step — on
  recovery. This is the concrete meaning of "complete previously interrupted work."
- Steps are individually idempotent:
  - `select_next_task` — pure function of board state; re-running yields the same task
    unless the board changed, and the recorded `output.taskId` is reused on resume.
  - `research` — keyed by `runId`; the report document id is `report_{runId}` so a retry
    overwrites rather than duplicating.
  - `post_report` — the session event carries `invocationId = runId:post_report`, and the
    step must make that tag actually deduplicate. **See the note below: the shipped session
    service does not do this for us.**
  - `propose_tasks` — each created task carries `createdByRun: runId` + a content hash;
    re-running skips tasks that already exist with that hash.
  - `reprioritize` — writing a fractional index is naturally idempotent.
> **Correction (verified against the pinned `google-adk==2.7.0` source): `append_event`
> does not deduplicate by `invocationId`.** The shipped
> `FirestoreSessionService.append_event` writes the event to `events/{event.id}` with
> `transaction.set()`, which creates-or-**overwrites** silently, and it never reads or
> compares `invocationId`. Tagging the event is therefore documentation, not enforcement:
> a re-executed `post_report` step appends a *second* event with a fresh id and the
> transcript shows the report twice.
>
> `post_report` idempotency has to be implemented explicitly. Two workable options, both
> local to the step: give the event a **deterministic id** derived from
> `runId:post_report` so the overwrite lands on the same document, or do an **explicit
> existence check** for that `invocationId` inside the appending transaction before
> writing. The deterministic-id option is the cheaper of the two and rides on the
> overwrite semantics that already exist.
>
> This is an **M5 obligation**, not something to build now — the step it protects does not
> exist until then. It is called out here because the "steps are individually idempotent"
> list above would otherwise read as already-satisfied. Note also that the report document
> (`report_{runId}`) *is* genuinely idempotent by the same overwrite mechanism; it is only
> the session event that needs the extra work. Overriding `append_event` is already the
> most bump-sensitive surface in the project
> ([03-agent-design.md](03-agent-design.md#bumping-the-adk-version)), so whichever option
> is chosen belongs on the bump checklist.

- Retries: Cloud Tasks retry with exponential backoff (min 30 s, max 10 min, 3 attempts).
  After `maxAttempts` the run is `failed` and surfaced in the UI as "the coach couldn't
  prepare this task — retry?" rather than silently disappearing.
- Poison-pill protection: a step that fails with a non-retryable error (invalid task
  state, quota exceeded, safety block) marks the run `failed` immediately without burning
  retries.

## What the run is allowed to change

Autonomous mode uses the reduced tool set from [03-agent-design.md](03-agent-design.md):

Allowed: `add_task` (≤ 5), `add_subtask`, `reorder_task`, `set_next_up`,
`post_research_report`, read-only tools.
Forbidden: `discard_task`, `set_task_state` to `completed`, `update_learner_profile`,
`update_project_prefs`, anything touching another project.

Every write records `origin: "agent"` and `runId`. On the user's next visit the board
shows an "Updated by your coach" banner listing what changed, with a one-click undo that
reverses the run's writes (the ledger records enough to reverse: created task ids and
previous `order`/`nextUpTaskId` values).

## Notifications

None in v1 (no email/push infrastructure). The next `GET /api/projects/{id}/tasks`
surfaces changes, and any connected client receives a `board_update` WebSocket frame in
real time. Email digests are a post-v1 item.

## Observability

Per run: structured log line at each step boundary with `runId`, `step`, `durationMs`,
`tokens`, `outcome`. Cloud Monitoring alerts on:

- `failed` run rate > 10 % over 1 h,
- runs stuck `running` past lease TTL (recovery not keeping up),
- daily token spend above threshold,
- Cloud Tasks queue depth > 100.

## Local development

`/internal/tick` is callable without OIDC when `ENV=local`, and `scripts/dev.sh` includes
a `tick` command plus a `--loop 60s` mode, so the whole autonomous path is exercisable on
a laptop against the Firestore emulator with no Cloud Scheduler or Cloud Tasks. In local
mode the Cloud Tasks enqueue is replaced by a direct in-process call behind the same
`JobQueue` interface.
