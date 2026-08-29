# Autonomous Updates

The requirement, restated as invariants:

1. Interrupted work is finished before new work is started.
2. Work happens on the next-up, non-completed, non-discarded task.
3. Research results are posted into that task's session.
4. The agent may add tasks and reorder them as a result.
5. **Auto-scheduled** work never runs on a project whose owner is currently working in
   that project. Work the learner *asked for* runs anyway — see below.

## Two kinds of work, and the only difference between them

Everything from the trigger chain down is shared. What differs is who decided the run
should happen, and that decides exactly two things: whether the owner being present skips
it, and where it sits in the queue.

| | **Auto-scheduled** | **Requested** |
| --- | --- | --- |
| Who decided | The coach, when it created the task with `needsResearch: true` | The learner, by pressing "Have my coach prepare this" on the task ([06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid)) |
| Marked by | `needsResearch == true` and `researchStatus ∈ {none, failed}` | `researchStatus == "pending"` with `researchRequestedAt` set ([02-data-model.md](02-data-model.md#projectsprojectidtaskstaskid)) |
| Owner present | **Skipped.** The learner is working; a background run that reshapes the board under them is the failure this guard exists for | **Runs.** Their press is why it exists, and pressing a button and watching nothing happen because you are looking at the page is indefensible |
| Cooldown, `autonomousEnabled`, quiet hours | All apply | **None apply.** All three are defaults about *unprompted* work, and this run was prompted |
| Lease, daily quota | Apply | Apply. The lease is mutual exclusion rather than policy, and the quota is a hard cost cap |
| Ledger `trigger` | `"scheduled"` | `"requested"` |
| Position in the tick's queue | After every requested run, `lastAutonomousRunAt ASC` | **First**, `researchRequestedAt ASC` |

A requested run is not a *manual* run. `POST /api/sessions/{sid}/research` (`trigger:
"manual"`, M4) is the learner pressing a button and watching the run's own research view.
A requested run is queued and headless: the learner marks the task and leaves, and the next
tick picks it up, without anyone watching. Both take the same lease, so the two cannot
collide.

**M4–M8: this was the direction of travel, not a side door.** `POST /api/sessions/{sid}/
research` executed inline — a detached `asyncio.Task` in the process that accepted the
request, streamed to a `turnId` the caller watched directly — while the headless,
tick-scheduled scheme was proven out on auto-scheduled and requested work. **Since M9, both
`trigger: "manual"` and `trigger: "requested"` are queued the same way**: a manual run's
`ResearchService` creates the ledger row and hands it straight to the same
`JobQueue`/`RunExecutor` a scheduled run goes through, rather than running its own turn in
the request. What still tells the two apart is *when* the queue picks the run up — a manual
run is enqueued directly, at accept time, and typically starts in about as long as a Cloud
Tasks dispatch takes; a requested run only becomes a run at the next `/internal/tick`, up
to 15 minutes later — not whether either one runs inline. The button the learner presses is
unchanged; what happens after the 202 is not.

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

A project is a candidate if every guard that applies to it holds. The last column is the
whole of the difference between the two kinds:

| Guard | Check | Applies to |
| --- | --- | --- |
| Owner enabled autonomy | `user.globalPrefs.autonomousEnabled` | auto-scheduled only |
| Not quiet hours | Current time in the user's timezone outside `autonomousQuietHours` | auto-scheduled only |
| Cooldown | `now - project.lastAutonomousRunAt > 6 h` | auto-scheduled only |
| **Owner not present** | `presence/{ownerUid}` does not show `activeProjectId == projectId` with a heartbeat in the last 120 s | auto-scheduled only |
| Project active | `project.status == "active"` | both |
| No lease held | `projects/{id}/locks/agent` absent or expired | both |
| Under run-count quota | `usage/{uid}_{today}.autonomousRuns < plan.limits.autonomousRunsPerDay` | both |
| **Under points quota** (M8-quotas) | Neither the monthly nor the 4-hour point window is exhausted — same check `TurnService.start` makes before any turn, applied here so a project that cannot afford the run is never scheduled in the first place ([02-data-model.md](02-data-model.md#usage-quotas-m8-quotas)) | both |
| **Above the run-start points threshold** (M10) | Remaining *monthly* points are at least `owner.plan.limits.runStartPointsThreshold` — refused earlier than outright exhaustion above, so a run is not started only to spend down toward a window it cannot finish inside of. `ResearchService`'s manual/roadmap triggers make the identical check (`QuotaService.require_room_to_start_run`) before enqueuing, so a learner pressing the button gets the same answer the tick would | both |
| Work exists | Auto-scheduled: at least one task in `draft`/`not_started`/`in_progress` with `needsResearch` and `researchStatus ∈ {none, failed}`. Requested: at least one task in those states with `researchStatus == "pending"` | both, differently |

`draft` is first in that list on purpose. From M4 it is the state a task starts in and the
one it leaves when research gives it a checklist ([02-data-model.md](02-data-model.md#task-state-machine)),
so a project full of drafts is exactly the project with the most for a run to do — and a
guard written before `draft` existed would have skipped it as having no work.

**The four owner-facing guards are skipped for a requested run rather than passed.** The
distinction matters for the log line: a requested run records which guards it bypassed, so
"why did my coach work at 2 a.m. when I set quiet hours" has an answer that names the press
rather than looking like a bug in the quiet-hours check.

Ordering of candidates: **every project with a requested task first**, by that project's
oldest `researchRequestedAt` ascending, then the rest by `lastAutonomousRunAt ASC`
(fairness). Capped at N projects per tick to bound cost — and the cap is applied *after*
the sort, so a backlog of auto-scheduled work can never starve a learner who pressed a
button.

Selecting the task **within** a chosen project follows the same rule: a requested task
outranks `select_next_task`'s ordinary lowest-`order` choice, oldest request first.
Otherwise a tick would take the project because the learner asked and then research
something else in it.

### Presence guard details

Presence is written from the WebSocket: every `presence` frame updates
`presence/{uid}.{activeProjectId, activeTaskId, lastHeartbeatAt}`; disconnect decrements
`connections` and, at zero, clears `activeProjectId`. The guard is checked **twice** —
once at scheduling time and again inside the lease transaction at execution time —
because a Cloud Tasks delivery can arrive minutes after scheduling and the user may have
sat down in the meantime. If the second check fails, the run is abandoned with
`status: "skipped_owner_present"`, not failed.

**Both checks are on the auto-scheduled path only.** A `trigger: "requested"` run does not
consult presence at either point, and the second check is where that has to be written
down rather than inferred: the execution-time guard runs inside the lease transaction,
minutes after the tick, with none of the tick's reasoning in scope. A requested run whose
learner sat down in the meantime is the *expected* case, not the race.

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
  "trigger": "scheduled" | "requested" | "manual",
  "mode": "queued",                     // "inline" is a pre-M9 value only; nothing writes it anymore
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
  "turnId": "t_…",                      // absent until the queue actually starts the turn
  "sessionId": "…",                     // + since M8: the research step's own dedicated
                                         //   session, not the task's conversation session
  "pendingText": "…",                   // + M9, manual/roadmap only: the opening message,
                                         //   set at accept time, read back when the turn starts
  "createdAt": ts, "updatedAt": ts,
  "expiresAt": ts,                      // the Firestore TTL field — 60 days out, refreshed
                                         //   on every write; see docs/02-data-model.md#retention
  "error": null
}
```

`taskId` is `null` for a run whose research is about the project as a whole rather than
one task — a manual run only, started from the project coach's conversation
([04-api-contract.md](04-api-contract.md#post-apisessionssidresearch)). `turnId` is written
once the queue starts the turn; absent until then. **Since M9, `sessionId` for a manual or
roadmap run is written earlier than that** — `ResearchService` creates the session at
accept time, before the run is even enqueued, so the 202 body already carries it; for a
scheduled or requested run, `sessionId` is still written by the `research` step itself, the
first time the executor actually runs it. **Since M9 the `research` step drives
`research_workflow` — the whole `research_planner` → `topic_researcher` × 3–5 →
`reviewer_writer` graph — instead of the single `research_agent` turn it drove before**
([03-agent-design.md](03-agent-design.md#the-research-pipeline-since-m9)); the ledger's own
step shape does not change, because `Workflow` has no "run one node and stop" primitive to
split it against — see
[03-agent-design.md](03-agent-design.md#workflow-as-a-turn-root-and-retrying-a-crash-mid-fan-out)
for why the ledger stays a single `research` step rather than one per pipeline node.

### Execution semantics

- The executor loads the run, resumes at `cursor`, and runs steps in order.
- **Each step commits its own output to the ledger before the next begins.** A crash
  during `propose_tasks` therefore never repeats `research` — the expensive step — on
  recovery. This is the concrete meaning of "complete previously interrupted work." Since
  M9, a crash *inside* the `research` step (mid-fan-out) is **safe** to retry but not
  **cheap**: `RunExecutor`'s retry is a new turn into the *same* session but with a fresh
  ADK `invocation_id`, and `research_workflow`'s own replay only recovers events tagged
  with the invocation *being resumed* — a different, prior invocation's checkpoints are
  invisible to it (`workflow/utils/_replay_manager.py`'s `_build_event_index`). So
  `research_planner` runs again on a retried step, verified by running the retry rather
  than assumed from the class's docstring
  (`tests/test_run_executor.py::test_a_crash_mid_fan_out_retries_the_whole_research_step_safely`);
  what the ledger still guarantees is that the retry produces exactly one report, not two,
  via `post_research_report`'s existing `report_{runId}` keying below.
- Steps are individually idempotent:
  - `select_next_task` — pure function of board state; re-running yields the same task
    unless the board changed, and the recorded `output.taskId` is reused on resume. It
    also records `output.requested`, because by the time a later step or a recovery asks,
    the flag it selected on is gone (below).
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
> **Settled at M5, and the shape turned out to be different from either option.** There is
> no `research_report_ref` event to give a deterministic id to: a research run is an
> ordinary turn, so what lands in the transcript is ADK's own `post_research_report` call
> and its function response, written by `append_event` on a path we do not choose ids for.
> What M5 implemented instead is the half that *is* ours:
>
> - The report document is keyed `report_{runId}` — the tool reads the run id out of
>   `temp:coach_run_id`, which the executor passes as `state_delta` — so a retried step
>   overwrites rather than duplicating. `tests/test_run_executor.py` asserts one report per
>   run across a deliberately re-executed step. Since M9 `post_research_report` is called by
>   `reviewer_writer`, the last node of `research_workflow`'s graph, rather than by
>   `research_agent` directly — the id derivation is unchanged.
> - **Resume-at-cursor is the real protection**, and it is stronger than event-level
>   dedup: a `research` step that completed is never re-entered at all, so the ordinary
>   crash-and-recover path writes nothing twice.
>
> What remains uncovered is narrow and worth naming: a step that posts its report and *then*
> fails — while streaming its closing prose — is retried, and the transcript gains a second
> tool call and response for the same report. The report and the checklist are unaffected;
> the conversation reads as though the coach delivered the same materials twice. Left as is
> rather than papered over, because the fix is either overriding `append_event` — the most
> bump-sensitive surface in the project
> ([03-agent-design.md](03-agent-design.md#bumping-the-adk-version)) — or making the reader
> deduplicate, and neither is worth it for a duplicate that costs nothing but a repeated
> line.

- Retries: Cloud Tasks retry with exponential backoff (min 30 s, max 10 min, 3 attempts).
  After `maxAttempts` the run is `failed` and surfaced in the UI as "the coach couldn't
  prepare this task — retry?" rather than silently disappearing.
- Poison-pill protection: a step that fails with a non-retryable error (invalid task
  state, quota exceeded, safety block) marks the run `failed` immediately without burning
  retries.

### When the request flag is cleared

A requested task's `researchStatus` moves `pending → in_progress` **as the run starts**, in
the same write that clears `researchRequestedAt`. Not at the end, and not on success only:

- The queue query is `researchStatus == "pending"`, so leaving the flag up while the run
  executes means the next tick — 15 minutes later, possibly against a still-running run —
  finds the same task and enqueues it again. The lease would refuse the second run, which
  makes it a wasted enqueue rather than a duplicate report, but a queue that keeps
  re-offering work it has already handed out is a queue that hides its own backlog.
- Clearing it on *failure* too is what stops a task the research agent cannot handle from
  being retried forever. `maxAttempts` bounds the retries of one run; without this, the
  ledger would give up and the tick would immediately queue a fresh one. A failed run ends
  with `researchStatus: "failed"` and no request, which is exactly the state the UI offers
  a "retry?" against
  ([06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid)) — the
  retry re-queues, and the decision to spend another run is the learner's.

The learner may also cancel a request before it is picked up, which writes `researchStatus`
back to `none` and clears the timestamp. Cancelling is only meaningful while the status is
`pending`; once a run holds the task, the way to stop it is the turn's cancel.

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

`dev.sh tick` calls the API that `dev.sh up` is already serving rather than starting one of
its own: with `ENV=local` the runs execute *inside* the process that received the tick, so
a second process would do the work somewhere nothing is watching.

**The local queue enforces the same concurrency limit the real one does** — five runs at a
time, matching the Cloud Tasks queue's `max_concurrent_dispatches`. That is not a detail of
the double: a stand-in without the limit its original enforces is a different system that
happens to compile, and this one's absence surfaced as the e2e suite degrading over
consecutive runs with `Aborted: Transaction lock timeout` on writes that had nothing to do
with any run ([09-roadmap.md](09-roadmap.md#four-more-rows-for-the-table-above)).
