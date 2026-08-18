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
| Resume cross-instance | Two app instances sharing the emulator; resume on B for a turn owned by A → the follower path delivers the remainder (it polls rather than listens; [09-roadmap.md](09-roadmap.md#status-after-m2)) |
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
- **Markdown rendering** — a GFM table becomes a `<table>`, `$…$` becomes KaTeX output, a
  fenced block keeps its code whether or not the highlighter ever loads, and raw HTML in a
  message stays inert text. Plus the streaming rule: a ` ```mermaid ` fence is a code block
  while its turn is generating and a diagram once it has settled
  ([06-frontend.md](06-frontend.md#markdown-in-the-transcript)). The dynamic imports are
  mocked, so the tests assert *this* module's decisions rather than shiki's and mermaid's
  output.
- **Composite task workspace** — a parent renders one card per subtask with its state
  actions, and none of them is a link; a leaf task renders no subtask block at all.
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

The suite runs on **four projects** — chromium, mobile-chrome, webkit, mobile-safari — and
every spec passes on each. WebKit became load-bearing at M2, when flow #4 arrived; it is
installed with `npx playwright install --with-deps webkit`
([07-infra-deploy.md](07-infra-deploy.md#python-dependency-pins-that-are-not-routine)).

**The model is stubbed by `MODEL_BACKEND=stub`, not by a stub server.** A server would have
to speak the Gemini wire protocol convincingly enough for `google.genai` to parse it, which
is a large surface to maintain for no extra confidence: nothing in the socket, the
checkpoint writer, or the resume path can tell where the tokens came from. The stub emits a
deterministic reply derived from the prompt, one word at a time with a configurable pause,
which is what gives flow #4 a window to disconnect inside *and* makes "identical to the
control run" a character-for-character assertion. It is refused for any `ENV` other than
`local` ([07-infra-deploy.md](07-infra-deploy.md#local-development)).

**From M3 it also emits scripted function calls**, and derives those from the prompt for
the same reason it derives its text from one: a canned call would assert nothing about the
prompt that produced it. It reads the task budget out of the *rendered system instruction*
and plans from *this turn's* function responses — the second being what makes the tool
loop terminate. Both are pinned by `tests/test_stub_model.py`, because a stub is one of
the three surfaces whose failure mode is silent success.

**And it answers one prompt in markdown** (`show me the formatting`), which is the only
way the transcript's renderer can be tested where it actually has to work.
`Markdown.test.tsx` mocks shiki and mermaid — it has to, or it would be asserting things
about someone else's grammar — so what no vitest run can see is whether those two
`import()` chunks are emitted, served, and resolved in a *built* bundle. `markdown.spec.ts`
asserts a rendered table, KaTeX output, a `code.shiki` token carrying both theme
variables, and an `<svg>` from mermaid, against the image the e2e stack serves.

**A `mermaid` fence is highlighted code for as long as the turn is streaming**, which is
the deferral rule and also a trap for the spec: shiki has a mermaid grammar, so two blocks
are highlighted for those seconds and a bare `code.shiki` selector is ambiguous exactly
until the turn settles. The spec waits for the diagram first, and scopes the rest by
language.

Sign-in is seeded rather than performed: the app under test runs with `ENV=local`, and the
Playwright fixture injects a `dev:<uid>` token so every flow starts authenticated. No flow
here is testing Google's sign-in popup, and seeding keeps the suite fast and deterministic.
Real token verification is covered by the nightly job below.

**Uploads are exercised for real, via a local-only PUT receiver.** `ENV=local` serves the
target of the signed URL itself (`api/routers/local_storage.py`), recording the bytes and
content type in the process's `InMemoryObjectStore`. Before that, the in-memory store handed
the browser a `https://storage.local/…` URL and no flow could complete an upload — the
picker, the drop zone, finalize, and the transcript had no end-to-end coverage at all, and
two defects reached a deployed environment through the gap (a missing `<Toaster />`, and
attachments vanishing from reopened conversations). The receiver is guarded and
regression-tested for every other `ENV`, exactly like the `Bearer dev:<uid>` path. Signing
is still not covered, and cannot be without a real signer.

**The transcript's reader is tested against generated vectors, not hand-written fixtures.**
`GET /api/sessions/{sid}/events` returns the serialized ADK `Event` verbatim, so
`apps/web/src/lib/transcript.ts` reads a shape this project does not define.
`scripts/gen_event_vectors.py` dumps real events exactly as `append_event` stores them into
`session-event-vectors.json`, which `transcript.test.ts` replays — the same
generated-parity approach as the fractional-index vectors, and for the same reason. It was
added after invented fixtures passed while the code was wrong: `Event` declares camelCase
aliases, but `model_dump()` defaults to `by_alias=False`, so the stored keys are
`file_data` and `mime_type`. Regenerate with `./scripts/dev.sh gen-event-vectors` after an
ADK bump.

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

   **Hold the reconnect's ticket, or the assertion races the banner.** The reconnecting
   state lasts `backoffDelay(0)` — a random 0–500 ms ([06-frontend.md](06-frontend.md#websocket-client))
   — plus a local round trip, which on a fast machine is regularly shorter than one
   polling interval. The banner renders correctly and the assertion misses it, so the
   test fails intermittently on whichever browser happened to be quickest; the response
   to an intermittent test is to re-run it rather than to look, which is why this one is
   made deterministic instead. Delaying the reconnect's `POST /api/ws-ticket` by a fixed
   1.5 s also makes the disconnect *mean* something — generation carries on with nobody
   attached for a window the test chose.

   The same discipline applies to the cancel flow: it sends a long prompt — the stub
   echoes it, so a long prompt is a long reply — and waits for text on screen before
   clicking, so the click races neither the 202 nor the end of generation. **Asserting on
   a transient state means owning how long it lasts.**

   That spec also found a real defect, and the sequence is worth keeping. It failed only
   on the two *mobile* projects, roughly one run in eight, with the click reported as
   successful and the handler never running — because `Transcript` scrolled itself to the
   bottom on every delta with `scrollIntoView`, which scrolls **every** scrollable
   ancestor including the document. On a viewport too short to hold the whole screen the
   page moved several times a second, so Playwright scrolled the button into view and the
   app scrolled it back out before the click landed. A mobile-only, timing-shaped failure
   whose cause was neither mobile-specific nor a timing bug: the two-pane layout is
   "stacked on mobile" ([06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid)),
   and stacked meant unbounded. **Suspect the page scrolling before suspecting the clock.**

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
