# Risks & Open Questions

## Risks

### R1 — ADK version bumps move the session/memory internals (mitigated)

**Important: `google-adk` is pinned at `2.7.0`. Do not bump it unless absolutely
necessary.**

Sessions, events, and memory are persisted by subclassing ADK's shipped
`FirestoreSessionService` and `FirestoreMemoryService`
([03-agent-design.md](03-agent-design.md)). Subclassing buys a lot of correctness for free —
async client, state-delta scoping, `revision`-based optimistic concurrency — but it couples
this project to those classes' **internals** rather than to a stable abstract interface.

*Impact:* the sharp edge is `append_event`. There is no extension point inside its
transaction, so adding the event `seq` means reimplementing the body. An upstream change to
the `revision` check, the state-delta split, or the event document shape will not conflict —
it will simply be silently ignored by our override, which keeps running the old logic.
*Mitigation:* the pin; the shared contract suite in [08-testing.md](08-testing.md), which
runs identical tests against our implementation and ADK's in-memory reference so a moved
semantic fails on the in-memory side first and names itself; and a bump checklist whose
first item is diffing the shipped `firestore_session_service.py` against the revision our
override was derived from
([03-agent-design.md](03-agent-design.md#bumping-the-adk-version)) — because that one
surface is exactly what the contract suite cannot see.
*Exits if the subclass stops earning its place:* use the shipped class unmodified and page
the transcript by `timestamp` rather than `seq`; or `DatabaseSessionService` against Cloud
SQL Postgres, which costs a managed database and a connection pool on Cloud Run and gives up
the single-datastore property. Neither is right for v1, but both are real.

### R2 — Built-in `google_search` cannot be mixed with function tools

A known Gemini/ADK constraint. Design already isolates search in a `search_agent` exposed
via `AgentTool`, which costs one extra model hop per research turn.
*Mitigation:* the M1 spike verifies whether the restriction still holds on `gemini-3.7-flash`.
If lifted, collapse the hop — a pure simplification with no other design impact.

### R3 — Cross-instance turn resume degrades to Firestore polling

Session affinity is best-effort. A reconnect can land on an instance that does not own the
running turn, in which case streaming falls back to a Firestore snapshot listener at
checkpoint granularity (~400 ms) rather than token granularity.
*Impact:* visibly chunkier streaming in a rare case. Not a correctness problem — no
inference is lost, no content is dropped.
*Escalation path:* Memorystore Redis pub/sub as the broker backplane, making every
instance able to serve any turn's live stream. Deliberately deferred: it adds a VPC
connector, a managed service, and a failure mode, to fix a cosmetic issue on a rare path.

### R4 — Cost overrun from autonomous research

Grounded research turns with `thinking_level: high` are the expensive operation, and the
scheduler runs them unattended. An unnoticed loop or an enthusiastic `propose_tasks` step
can multiply spend.
*Mitigations already in the design:* 6-hour per-project cooldown, per-user daily run caps,
≤ 5 new tasks per run, per-run tool-call and token ceilings, `usage/*` counters,
a Cloud Monitoring alert on daily spend, and a GCP billing budget alert. Add a hard kill
switch (`autonomousEnabled` at the app level) before opening signups.

### R5 — Research quality and link rot

The agent can recommend a dead link, a paywalled article, or a video that doesn't cover
what the task needs.
*Mitigations:* `fetch_url` verification of the top candidates before they enter `required[]`
(title-based selection is how bad reading lists happen); YouTube duration and metadata
from the API rather than the model; a thumbs-down control on each item that feeds the
learner profile and re-triggers research.

### R6 — The agent quietly reshapes the user's board

Background autonomy that adds and reorders tasks can feel like losing control of your own
plan.
*Mitigations:* the reduced autonomous tool set (no discard, no completing, no preference
edits), `origin: "agent"` badges, the "Updated by your coach" banner, and per-run undo.
`autonomousEnabled` is a first-class user preference, and quiet hours are respected.

### R7 — Prompt injection via fetched pages and uploads

`fetch_url` and user uploads put untrusted text in front of a tool-calling model.
*Mitigations:* fetched content is wrapped in explicit untrusted-content delimiters with an
instruction that it is data, never instructions; tools that mutate state are unavailable to
`research_agent` except `post_research_report`; all mutations are scoped to the current
project by the service layer regardless of what the model asks for; SSRF guards on the
fetcher. Server-side authorization, not prompt discipline, is the actual control.

### R8 — Cloud Run scale-to-zero vs. background work

With `cpu_idle = false` and `min-instances = 1` this is handled, but the failure mode if
either setting regresses is subtle: generation stalls only for disconnected clients, which
no ordinary test would catch.
*Mitigation:* Terraform-enforced, plus an assertion in the deploy smoke test that reads the
live revision config and fails the deploy if `cpu_idle` is true or `min_instances` is 0.

### R9 — Firestore write volume from checkpoints

Every turn writes checkpoints plus final events. At scale this is the dominant Firestore
cost.
*Mitigation:* 400 ms / 512-char batching, no persistence of partial ADK events, 7-day TTL
on `turns/*`. Monitor writes per turn; if it exceeds ~30, increase the batch window.

### R10 — Model version drift

`gemini-3.7-flash` will be superseded, and Gemini 3.x already changed generation config in
breaking ways (`thinking_level` replacing `thinking_budget`; `temperature`/`top_p`/`top_k`
and `candidate_count` unsupported; `call_id` + `name` required on every `FunctionResponse`).
*Mitigation:* model id and generation config live in one `ModelConfig`; the evalset suite
is the regression gate for a model bump; pin an explicit version rather than a floating
alias in production.

## Open questions

These do not block M0–M2. Each needs an answer before the milestone named.

**Q1–Q6 are settled — each took its default.** Q1–Q3 were due at M3, Q4 at M4, and Q5–Q6
at M5; the last two were answered early, at M4, because the research path M4 builds is the
one M5 schedules and it is cheaper to build it against a decided cadence than to leave the
guards parameterised on an open question. Where a default is a rule about what the agent
may do, it is enforced in `agents/tools.py` rather than in the instruction, on the same
reasoning as `discard_task`'s confirmation gate: a rule the model can decline to follow is
not a rule.

| # | Question | Needed by | Default if unanswered |
| --- | --- | --- | --- |
| Q1 | Should the coach ever mark a task complete on its own (e.g. after grading a submitted exercise), or is completion always the user's click? | ~~M3~~ **settled** | User's click only. The agent may *suggest* completion. `set_task_state` refuses `completed` — and refuses `discarded` too, which would otherwise be a second route around the confirmation gate. See the note below on how M4's item checklist keeps this true. |
| Q2 | How deep should task nesting go? The plan assumes exactly one level (task → subtask). | ~~M3~~ **settled** | One level, enforced transactionally in `TaskService`. Deeper nesting complicates rollups, ordering, and the board for little gain. |
| Q3 | When the user's estimate and the agent's disagree (user says 30 min, agent says 90), who wins? | ~~M3~~ **settled** | User wins. Prompt behaviour rather than a guard, because the disagreement is a conversation: the coach flags it once, offers to split, and then works to their number. `update_task` may still change an estimate the learner asked it to change. |
| Q4 | Should research reports accumulate per task (history) or should a re-run replace the previous report? | ~~M4~~ **settled** | Accumulate, newest shown by default, older ones collapsible. Cheap and non-destructive. A re-run *replaces* the task's item checklist, though — see [02-data-model.md](02-data-model.md#task-items) — because two reports' worth of items is a checklist nobody can finish. |
| Q5 | Autonomous cadence — is every 15 minutes with a 6-hour per-project cooldown right, or should it be a nightly batch? | ~~M5~~ **settled** | The 15 min / 6 h combination. Revisit against real cost data at M7. |
| Q6 | Does the user need to see and approve agent-proposed tasks before they appear on the board, or do they appear directly with undo? | ~~M5~~ **settled** | Appear directly, badged, with undo. A pending-approval queue is more friction than an unread badge. |
| Q7 | Retention of session transcripts — indefinite, or a rolling window? | M7 | Indefinite; deleted on account deletion. Revisit if storage cost matters. |
| Q8 | Any content policy on what projects are allowed (the coach will research arbitrary web content on the user's behalf)? | M7 | Standard Gemini safety settings, no additional filtering. |
| Q9 | Is seeding sign-in with the `ENV=local` dev token good enough for e2e, or does the suite need real-shaped ID tokens locally? | M0 | Dev token plus the nightly real-sign-in test against `coach-dev`. Revisit only if a token-shape bug reaches dev. |

**Q1 and the item checklist.** M4 gives a leaf task an ordered list of items and completes
the task automatically once every item is done and no research is outstanding
([02-data-model.md](02-data-model.md#task-items)). That is not a reversal of Q1. An item is
marked done either by the learner's checkbox or by `complete_task_item`, which is gated with
ADK's `require_confirmation` exactly as `discard_task` is — so the last thing to happen
before a task completes is still a click. What changed is *which* button: the learner
confirms the last piece of work rather than the task as a whole. `set_task_state` still
refuses `completed`, so the agent has no path to the state itself.

## Things deliberately not in v1

Stated here so they read as decisions rather than omissions: billing and plans, project
sharing or teams, email/push notifications, voice sessions, mobile apps, offline support,
vector-based memory retrieval, multi-region deployment, and any model other than
`gemini-3.7-flash`.
