# Testing Strategy

The system's hard parts are concurrency, resumption, and nondeterministic model output.
The test strategy targets exactly those; everything else gets ordinary coverage.

## Backend (`apps/api`)

Stack: `pytest`, `pytest-asyncio`, `pytest-httpx`, `hypothesis` (for the ordering
algorithm), the gcloud Firestore emulator (`gcloud beta emulators firestore start`),
`freezegun` for lease/TTL clocks.

### Unit

- `resolve_prefs(global, project)` — full matrix of inherit/override, including the
  brief's example (global 45 min, project 120 min).
- Fractional index: property test that any sequence of inserts/reorders produces strictly
  increasing keys, and that the rebalance path preserves visible order.
- ISO-8601 duration parsing and the video-budget filter.
- `ResearchReport` validation: rejects `Σ required.minutes > budget`, rejects an item in
  both lists, requires `why` on every required item.
- SSRF guard on `fetch_url`: private ranges, redirect chains into private ranges,
  `file://`, oversized bodies, slow-loris timeouts.
- **`ENV=local` auth bypass is inert everywhere else.** Parametrized over every non-`local`
  `ENV` value: `Authorization: Bearer dev:someuid` must return `401`, and the dev branch
  must not be reachable. This is deliberate auth-bypass code
  ([04-api-contract.md](04-api-contract.md#authentication)), so it gets a named regression
  test rather than a comment and a hope.
- **Revocation checking is on exactly the two intended endpoints.** With `verify_id_token`
  patched, assert `check_revoked=True` for `POST /api/ws-ticket` and `DELETE /api/me`, and
  `False` for a representative hot-path route. This is a deliberate latency-vs-staleness
  trade ([04-api-contract.md](04-api-contract.md#authentication)); untested, it silently
  drifts to whatever default a future refactor picks — in either direction, since one way
  costs a round-trip per request and the other widens the staleness window on sockets.
- **SPA catch-all does not shadow the API.** Asserts `/api/*`, `/ws`, `/internal/*`,
  `/livez`, and `/readyz` all resolve to their handlers rather than `index.html`, and that
  an unknown path does serve the SPA. Guards the route-registration order that
  [07-infra-deploy.md](07-infra-deploy.md#container) depends on.

### Task state machine

A table-driven test over every (from_state, transition) pair asserting allowed/denied and
the resulting invariants — plus a concurrency test that two simultaneous `set_next_up`
calls leave exactly one `current` task (run against the emulator, real transactions).

### Rollups

Parent `rollup` correctness after: add subtask, edit estimate, complete subtask, delete
subtask, and a batch of concurrent subtask writes.

### Session service contract suite

One parametrized suite executed against **both** `InMemorySessionService` and our
`CoachSessionService`, asserting identical behaviour for: create/get/list/delete,
`append_event` state-delta scoping (session vs `user:` vs `app:` vs `temp:`),
`GetSessionConfig.num_recent_events` truncation, `get_user_state` without a session, and
event ordering under concurrent appends. Pairing our implementation against ADK's own
reference is what keeps the two behaviourally identical: if a semantic moves between pinned
versions, the shared suite fails on the in-memory side first and names what changed. This is
the gate for an ADK version bump
([03-agent-design.md](03-agent-design.md#bumping-the-adk-version)).

Same approach for `CoachMemoryService` against `InMemoryMemoryService`.

Because we subclass rather than implement from scratch, three assertions do **not** belong to
the shared suite — the in-memory reference has no opinion on them, so they are separate tests
against the emulator:

- **`seq` is gap-free and equals `revision`.** Append N events, assert `seq` is `1..N` with
  no holes, and that it survives an interleaved `get_session`. This is the invariant
  `?after_seq=` pagination rests on.
- **`StaleSessionError` still fires.** Load a session twice, append on both, assert the
  second raises. The override reimplements the shipped transaction, so this check is the
  thing most likely to be dropped in a careless edit — and losing it corrupts state silently
  rather than loudly.
- **Optional-item completion is refused.** `PATCH /api/reports/{id}/items/{itemId}` with
  `completed: true` on an item in `optional[]` returns `4xx`
  ([04-api-contract.md](04-api-contract.md)).

### Streaming and disconnect resilience (the critical suite)

Using `httpx.ASGITransport` + a fake WebSocket client, with a scripted fake model that
emits a known delta sequence at controlled intervals:

| Test | Assertion |
| --- | --- |
| Happy path | Client receives `seq` 0…N with no gaps, then `turn_complete` |
| **Disconnect mid-generation** | Socket closed at `seq=10`; the turn still reaches `status: complete`, all events land in Firestore, and the fake model was invoked exactly once |
| No subscribers at all | Turn started, socket never opened → still completes and checkpoints |
| Resume same instance | Reconnect with `lastSeq=10` → replays 11…N with no duplicates and no gaps |
| Resume after completion | Reconnect after `turn_complete` → full replay from checkpoints, then `turn_complete` |
| Resume cross-instance | Two app instances sharing the emulator; resume on B for a turn owned by A → Firestore-listener path delivers the remainder |
| Explicit cancel | Cancel endpoint stops generation, marks `cancelled`, notifies subscribers |
| SIGTERM drain | In-flight turn is awaited within the grace period; a turn that outlives it is marked `failed, retryable` |
| Duplicate deltas | Deltas with `seq <= lastSeq` are dropped client-side (also covered in web tests) |

### Autonomous runs

- Recovery: a run written as `running` with an expired lease is re-enqueued by `/internal/tick`
  and resumes at `cursor` — asserting the completed `research` step is **not** re-executed.
- Idempotency: executing the same `runId` twice produces one report document, one session
  event, and no duplicate tasks.
- Presence guard: owner presence written before scheduling → project skipped; presence
  written *between* scheduling and execution → run ends `skipped_owner_present`.
- Lease contention: a manual research request during an autonomous run returns `409` with
  the in-flight `runId`.
- Quota exhaustion → run not created, interactive turns unaffected.
- Postponement sweep: `postponed_until` in the past flips to `not_started`; in the future
  does not.

### Agent-level tests

Two tiers:

1. **Deterministic tool-contract tests** with a stubbed model that emits scripted function
   calls. Asserts that a `split_task` call produces valid subtasks respecting the duration
   budget, that autonomous mode's forbidden tools are actually unavailable, and that
   `post_research_report` writes the right documents and session event.
2. **ADK evalsets** (`adk eval`) run nightly, not per-PR, against the real model with
   recorded fixtures for search/YouTube. Scored on: does research output fit the budget;
   are required/optional correctly separated; is a 4-hour task split; does the coach ask
   before proposing a plan. Treated as a quality signal with a threshold, not a
   pass/fail gate on every commit — model nondeterminism in CI is a false-failure factory.

Web-search and YouTube calls are recorded fixtures (VCR-style) in all automated tests; a
`--live` flag hits the real APIs, used manually and nightly.

Coverage target: 85 % on `services/`, `repositories/`, `adk_firestore/`, and `ws/`.
Agents and prompts are excluded from the line-coverage target and covered by the tiers above.

## Frontend (`apps/web`)

Stack: Vitest, React Testing Library, MSW for HTTP, a fake WebSocket server for streaming.

- **Streaming reducer** — unit tests over the frame sequence: ordered deltas, out-of-order
  arrival, duplicates after resume, `turn_error`, and the Zustand→Query handoff on
  `turn_complete` (assert the transcript query is updated exactly once and the buffer is
  cleared).
- **Reconnect logic** — backoff schedule, ticket refresh, resume frames emitted for every
  running turn, presence heartbeat start/stop on visibility change.
- **Optimistic mutations** — complete/postpone/reorder patch the cache immediately and roll
  back on a 500; the optimistic fractional index equals the server's.
- **Board filters and rollups** — hide-completed default on; parent card renders
  "4 subtasks · 2 h 30 m" from `rollup`.
- **Research report rendering** — required and optional blocks are distinct landmarks;
  optional items render no completion checkbox; the budget meter sums only required items.
  This is a product requirement, so it gets an explicit regression test.
- **Theme resolution** — the full matrix of `pref` (`light`/`dark`/`system`) ×
  `prefers-color-scheme` (`light`/`dark`), asserting the resolved class on `<html>` and the
  `color-scheme` style. Plus: a `matchMedia` `change` event re-resolves when `pref` is
  `system` and is ignored when it is not; the choice survives a remount; and `localStorage`
  throwing (private mode) falls back to light instead of crashing the app.
- **Theme storage-key contract** — asserts `useThemeStore` writes a plain string to
  `localStorage['coach.theme']` in exactly the format the inline `index.html` script parses.
  These two are coupled across a boundary the type system cannot see
  ([06-frontend.md](06-frontend.md#theme-light-dark-system)), so the contract is pinned by a
  test rather than by a comment.
- Duration formatting edge cases (0, 45, 60, 90, 150 min).

## End-to-end (Playwright)

Runs against the gcloud Firestore emulator and a stubbed model server, so e2e is
deterministic. Chromium + WebKit; mobile viewport for the board.

**Why WebKit is not optional here.** Two of this project's load-bearing guarantees are
exactly the ones that diverge between engines:

- *Mobile is a WebKit story.* On iOS every browser is WebKit, and the board has explicit
  mobile requirements — a mobile viewport in this suite, and the task workspace's two panes
  "stacked on mobile" ([06-frontend.md](06-frontend.md)). Chromium-only testing would leave
  the mobile layout unverified on the engine most likely to render it.
- *The disconnect guarantee is engine-sensitive.* Golden flow #4 is the highest-value test in
  the suite, and it depends on socket teardown, background-tab throttling, page lifecycle,
  and `visibilitychange` — the presence heartbeat stops after 2 minutes hidden. Safari has
  historically differed from Chromium on all four. Verifying resume only on Chromium would
  verify it on the half of the population least likely to break it.

Secondary but real: streaming text uses `aria-live="polite"`, and VoiceOver's behaviour on
WebKit differs from Chromium's accessibility tree.

WebKit is first *required* at M2, so installing it is deferred until then
([07-infra-deploy.md](07-infra-deploy.md)). As of M2 the suite runs on four projects —
chromium, mobile-chrome, webkit, mobile-safari — and flow #4 passes on all four.

**The model is stubbed by `MODEL_BACKEND=stub`, not by a stub server.** A server would have
to speak the Gemini wire protocol convincingly enough for `google.genai` to parse it, which
is a large surface to maintain for no extra confidence: nothing in the socket, the
checkpoint writer, or the resume path can tell where the tokens came from. The stub emits a
deterministic reply derived from the prompt, one word at a time with a configurable pause,
which is what gives flow #4 a window to disconnect inside *and* makes "identical to the
control run" a character-for-character assertion. It is refused for any `ENV` other than
`local` ([07-infra-deploy.md](07-infra-deploy.md#local-development)).

Sign-in is seeded rather than performed: the app under test runs with `ENV=local`, and the
Playwright fixture injects a `dev:<uid>` token so every flow starts authenticated. No flow
here is testing Google's sign-in popup, and seeding keeps the suite fast and deterministic.
Real token verification is covered by the nightly job below.

Golden flows:

1. **Create project → Socratic intake → first task list exists.**
2. **Big task gets split** — user asks for a 4-hour task; the coach splits it; the parent
   card shows subtask count and summed duration.
3. **Work a task end to end** — open workspace, upload a screenshot, get feedback, mark
   complete; board updates; completed task hidden by the default filter.
4. **Disconnect and resume** — start a turn, kill the WebSocket mid-stream with
   `page.routeWebSocket()`, confirm the "still working" UI, let the socket reconnect, assert
   the completed message is identical to the un-interrupted control run. *This is the
   highest-value e2e test in the suite — it verifies the one guarantee that is invisible
   from the UI when it works and catastrophic when it silently doesn't.*

   **Use `routeWebSocket`, not a CDP session.** `newCDPSession()` is Chromium-only and
   throws on WebKit — so specifying flow #4 in terms of CDP would make it unrunnable on the
   exact engine the section below argues it most needs to run on. `page.routeWebSocket()`
   works on both engines and is also the more precise instrument: it drops the socket while
   leaving REST requests alive, which is the scenario under test. `context.setOffline()` is
   the fallback when the test wants the whole network gone, but it is a blunter tool — it
   also kills the `POST /turns` and the transcript refetch, so a passing test proves less.
5. **Manual research trigger** — user creates a task, clicks "Research this task now",
   sees progress chips, and the report renders with required/optional separation.
6. **Autonomous update visible on return** — trigger `/internal/tick` from the test,
   assert a `board_update` arrives on an open board and the "Updated by your coach" banner
   lists the change, and that undo reverses it.
7. **Preference adaptation** — set project task duration to 2 h; new agent-created tasks
   respect it while another project still uses the 45-minute global default.
8. **Presence guard** — with a client connected to project A, a tick creates no run for A
   but does for project B.

## CI wiring

- Per PR: unit + integration + web unit + e2e against stubs. Target < 10 minutes.
- Nightly: ADK evalsets against the live model, live-API integration tests, Playwright
  against the deployed dev environment, and a Terraform plan drift check.
- Nightly, **real auth path**: one test signs in to `coach-dev` through Identity Platform
  with a dedicated test account and calls `/api/me` with the resulting token, exercising
  `verify_id_token` for real. This is the compensating control for seeding sign-in in the
  PR suite — without it, nothing would ever prove production token verification works.
- Load check before launch: 50 concurrent streaming turns to validate the per-instance
  semaphore, memory ceiling, and checkpoint write volume.
