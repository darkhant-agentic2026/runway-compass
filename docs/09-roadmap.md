# Roadmap

Eight milestones. Each has a demoable outcome and explicit exit criteria. Sizes assume one
focused developer; treat them as sequencing signal, not commitments.

---

## M0 — Foundations (~1 week)

Repo skeleton, both apps building, one deploy pipeline working end to end.

- Monorepo layout; `apps/api` with `uv`, `ruff`, `pytest`, Pydantic `Settings`; `apps/web`
  with Vite, TS strict, Tailwind, shadcn init, React Router shell.
- Lock the dependency floor: `google-adk==2.7.0` (verified to ship the Python Firestore
  session/memory pair this design subclasses), and a JRE 21+ on every dev machine and CI
  runner — without it the Firestore emulator refuses to start and no backend test can run
  ([07-infra-deploy.md](07-infra-deploy.md#prerequisites)).
- One-time per environment (the only non-Terraform steps — see
  [07-infra-deploy.md](07-infra-deploy.md)): enable Identity Platform in the Cloud
  Marketplace, and create the OAuth 2.0 web client + consent screen.
- Terraform: GCP projects, Firestore database, Artifact Registry, Cloud Run service,
  service accounts, Workload Identity Federation for GitHub, Identity Platform + Google
  provider.
- `ci.yml` + `deploy-cloudrun.yml`. The SPA is built into the API image, so there is no
  separate web deploy.
- Identity Platform sign-in; `/api/me` returning the verified principal; login screen;
  the `ENV=local` dev-token path and its inert-elsewhere test.
- `scripts/dev.sh up` runs the Firestore emulator + API + web locally.

**Exit:** a signed-in user sees their email on a deployed dev URL; a green PR pipeline;
`terraform apply` reproduces the environment from scratch given the two bootstrap steps
above; the deployed service serves both the SPA and the API from one origin.

---

## M1 — Domain core, no agent (~1.5 weeks)

The task board works as a plain CRUD app. Getting this right before adding an LLM means
agent bugs are later distinguishable from data bugs.

- Firestore repositories + service layer; `Principal` authorization on every path.
- Projects and tasks CRUD; task state machine with transactional invariants; fractional
  index ordering; parent rollups.
- Board UI: project list, task board, filters, drag-and-drop reorder, row actions,
  optimistic mutations.
- Global + project preferences with `resolve_prefs` and the settings screens.
- Theme switching (light/dark/system) with the no-flash inline script
  ([06-frontend.md](06-frontend.md#theme-light-dark-system)). It lands here rather than at
  M7 because the inline script and `color-scheme` handling are cheap now and awkward to
  retrofit once every screen exists — and because building the remaining screens against
  both themes is far cheaper than auditing them into a second theme later.
- **Spike (timeboxed, 1 day): confirm, against the pinned ADK version, whether the built-in
  `google_search` tool can be combined with custom function tools in a single agent.** The
  answer decides whether `search_agent` stays a separate `AgentTool` hop
  ([03-agent-design.md](03-agent-design.md)).

**Exit:** a user can manage projects and tasks entirely by hand; state machine and rollup
test suites green; the ADK tool-mixing question is answered and recorded.

---

## Status after M0 and M1

Both milestones are complete and deployed to `coach-dev`. What follows is the carry-over
a later milestone needs, recorded here because it is not derivable from the code.

**Met.** A signed-in user sees their email on the deployed dev URL; the SPA and API are
served from one origin; the board works end to end by hand; `ci.yml` and
`deploy-cloudrun.yml` both run green on merge to `main`. 189 backend tests, 105 web, 18
Playwright specs.

Playwright now runs on **four projects** — chromium, mobile-chrome, webkit, and
mobile-safari — and all 18 specs pass on each. The M1 board, theme, and drag-and-drop are
WebKit-clean before M2 starts, which is the point of installing it early.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| `min_instances` back to **1** in `envs/dev/dev.tfvars` | **M2** | Currently 0 to save idle cost. From M2 a scaled-to-zero instance can be reaped mid-generation, which is the failure the disconnect guarantee exists to prevent |
| Nightly **Terraform plan** drift check | soon | CI cannot run `plan` — the deploy workflow has no state-bucket access by design ([07-infra-deploy.md](07-infra-deploy.md#ci-does-not-run-terraform)). Nothing currently detects infrastructure drift |
| Nightly evalsets, live-API tests, real-auth test | M4–M6 | Specified in [08-testing.md](08-testing.md#ci-wiring), none implemented |
| `prod` environment | before any release | No GCP project, no GitHub Environment. Note that environment protection rules need a paid plan on private repos ([RUNBOOK](../infra/terraform/RUNBOOK.md)) |
| `terraform destroy` / from-scratch reproducibility | before relying on it | M0's other exit criterion, never exercised. `google_identity_platform_config` likely cannot be deleted, so a re-apply would need the import again |

**Endpoints in the API contract that are not implemented yet**, all by milestone rather
than oversight: the intake session created by `POST /api/projects` (M2), everything under
sessions, turns, uploads, and runs (M2–M5), `PATCH /api/me/learner-profile`'s Settings UI
(M6), and `DELETE /api/me` (M7).

**Decisions made during implementation** that the design documents did not fix, each
commented where it lives: the fractional index uses base-62 keys rather than a literal
LexoRank string ([02-data-model.md](02-data-model.md#ordering) calls the format
illustrative); `idempotency/*` and the `id` field on task documents were added to the
collection map; shadcn's current registry builds on Base UI rather than Radix, so the
theme control has `aria-pressed` toggle semantics rather than `radiogroup`
([06-frontend.md](06-frontend.md#the-control) specifies the latter).

---

## M2 — Sessions, streaming, and the disconnect guarantee (~2 weeks)

The riskiest engineering in the project. Do it early, with a stub model.

- `CoachSessionService` (subclassing ADK's shipped `FirestoreSessionService`) + the shared
  contract suite against `InMemorySessionService`, plus the `seq`-is-gap-free and
  `StaleSessionError` tests that the shared suite cannot cover.
- `TurnRegistry`, `StreamBroker`, checkpoint writer, detached generation tasks.
- WebSocket endpoint, ticket auth, subscribe/resume/presence frames.
- ADK `Runner` wired to `gemini-3.7-flash` with a minimal coach agent (no domain tools yet);
  multimodal uploads via signed GCS URLs and `GcsArtifactService`.
- Frontend socket module, `useStreamStore`, chat UI, reconnect + resume, "still working" state.
- Cloud Run settings: `cpu_idle = false`, `min-instances = 1`, session affinity, 3600 s timeout.

**Exit:** the full disconnect matrix from [08-testing.md](08-testing.md) is green,
including cross-instance resume; Playwright flow #4 passes; a user can chat with the coach
about an uploaded screenshot on the deployed dev environment.

---

## Status after M2

Complete and deployed to `coach-dev`. What follows is the carry-over a later milestone
needs, recorded here because it is not derivable from the code.

**Met.** The full disconnect matrix from [08-testing.md](08-testing.md) is green, including
cross-instance resume — driven by two `Container`s over one emulator, which is what a
second Cloud Run instance is. Golden flow #4 passes on all four Playwright projects, which
is what installing WebKit early was for. The last criterion was verified by hand on
`coach-dev` on 2026-08-17: chat with the coach, attach a screenshot by paperclip and by
drag-and-drop, get a reply that demonstrably sees it, reload, and take another turn in the
same session. 318 backend tests, 178 web, 76 Playwright specs.

**Fixed after the merge, on `coach-dev`.** The deployed revision failed the first turn of
every conversation — `1 validation error for InvocationContext … Input should be an
instance of BaseArtifactService` — because the artifact service was deferred behind
`LazyProxy` and ADK type-checks that field. The deferral is now a provider
(`integrations/artifacts.artifact_service_provider`): a callable the container hands to
`UploadService` and `RunnerFactory`, which resolve it when a request needs the bucket, so
ADK receives the real `GcsArtifactService`. The same proxy also cost `artifact_part_uri`
its `_get_blob_name` lookup, **so any upload finalized on the broken revisions has an
`artifact://` URI in `uploads/{id}.artifactUri` and its attachment is invisible to the
model**; those rows are stale and were not migrated. Both are in the table below.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| Content **scanning** on `POST /api/uploads/{id}/finalize` | **M7** | The contract lists it in that step and nothing scans. An accepted MIME type and a size cap are the only checks on uploaded bytes |
| `subscribe` by `runId` | **M5** | The frame is accepted and answered with an explicit error until the run ledger exists |
| Tool-activity chips on **resume** | M3+ | Chips render from the live stream but are not checkpointed, so a resumed client rebuilds them from the finalized transcript rather than the stream |
| Composite indexes for the `turns` queries | **M5** | `list_running_for_instance` and `expire_stale` were written ahead of a caller, needed indexes that do not exist, and were deleted. The ledger sweep should add each query *with* its index and its row in the index table |
| Nightly evalsets, live-API tests, real-auth test | M4–M6 | Still as recorded after M1 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1. `envs/prod` also has a commented `vertex_location` needing its own decision |

**Endpoints in the API contract still unimplemented**, by milestone rather than oversight:
`POST /api/sessions/{sid}/research` and everything under reports (M4), runs (M5),
`PATCH /api/me/learner-profile`'s Settings UI (M6), and `DELETE /api/me` (M7).

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| Cross-instance resume **polls** `turns/{turnId}` every 400 ms instead of using a snapshot listener | [04-api-contract.md](04-api-contract.md#surviving-client-disconnects) says "snapshot listener", but `on_snapshot` exists only on the *synchronous* `DocumentReference`; the async one raises `NotImplementedError`, and the async client is not optional. Polling at the checkpoint interval delivers the granularity that section already accepts | `ws/manager.py` |
| `checkpoints[].lengths` on the turn document | Makes resume exact when `lastSeq` falls *inside* a slice, instead of duplicating or dropping text | `services/models.py` |
| `StreamBroker` keeps a per-turn ring buffer of recent frames | Deltas are published immediately but checkpointed up to 400 ms later; a client attaching in that window would find a frame in neither source | `ws/broker.py` |
| `ws_tickets/*` and `uploads/*` added to the collection map | Cross-instance state the contract needs and no existing collection holds | [02-data-model.md](02-data-model.md#collection-map) |
| **`MODEL_BACKEND=stub`**, refused for any `ENV` but `local` | [08-testing.md](08-testing.md) asks for "a stubbed model server"; a stubbed *model* is the same determinism for a fraction of the surface. Guarded like the `Bearer dev:<uid>` path — its failure mode is *silent success* | `integrations/stub_model.py` |
| A local-only **PUT receiver** for uploads, same guard | Without it the in-memory store hands the browser an unreachable URL and the entire upload path is untestable end to end | `api/routers/local_storage.py` |
| **`vertex_location`**, separate from the Cloud Run region | Model availability is per project *and* per location; `gemini-3.7-flash` is served to `coach-dev` on `global` only. Set in `envs/dev`, left unset in `prod` where `global` is a data-residency decision | [07-infra-deploy.md](07-infra-deploy.md#local-development) |
| An unhandled exception now answers in `problem+json` with a `traceId` | It was the one thing in the service returning `text/plain`, so every bug reached the user as "request failed" while its traceback sat unlinked in the logs. `detail` names the exception outside production only | `main.py` |
| Model errors are **classified**: 5xx and 408/409/425/429 retryable, other 4xx terminal | `retryable` drives "You can try again", and a 404 for a missing model invited a retry that could never work | `services/turns.py` |
| A cancelled turn is a `turn_error` with `code: "cancelled"`, `retryable: false` | The contract has no `turn_cancelled` frame, and the user asked for this | `services/turns.py` |
| `GET /api/turns/{turnId}` added | Lets a client with a dead socket tell a running turn from a finished one | `api/routers/sessions.py` |
| `GET /api/sessions/{sid}/events/{seq}/attachments/{index}` added | Backs the transcript's image previews. Addressed by *position*, so reaching an event proves ownership and no caller-supplied storage path is validated | `api/routers/sessions.py` |
| An upload's artifact is `user:`-scoped, named for its upload id | `POST /api/uploads` does not know a session, and ADK scopes an artifact to one or the other. The id rather than the filename, so two `screenshot.png`s are two artifacts and not two versions | `integrations/artifacts.py` |
| `google-cloud-storage` pinned explicitly | Imported directly but arriving only as a transitive of `firebase-admin` — the same argument that pins `google-cloud-firestore` | `pyproject.toml` |

**Known limitation: `gs://` attachments do not work against `MODEL_BACKEND=gemini_api`.**
[00-overview.md](00-overview.md#model-configuration) specifies attachments as `types.Part`
file references backed by GCS, which Vertex resolves and the Gemini API does not — it wants
inline bytes or its own Files API. Production is Vertex, so the shipped path is right; a
developer running locally against a real Gemini key will find text works and attachments do
not. Not worked around, because the workaround is a second multimodal path production would
never exercise.

### What a green local run does not prove

Nine defects were fixed closing this milestone and a tenth arrived after the merge, in the
deployed revision. **Eight were invisible to a fully green local gate** — the whole suite
passed against code that could not run in CI or in production. The individual incidents are
in the git history; what generalises is below, and M4 and M5 add query surface and
integrations to every row of it.

| Trap | How it presents | Where it will recur |
| --- | --- | --- |
| **The Firestore emulator does not enforce index requirements** | A query with two filters — or one filter on a collection group — passes every local test and returns `FAILED_PRECONDITION` on the first deployed call | M4's report queries, M5's run-ledger and sweep queries. Add the query, the index, and its row in [02-data-model.md](02-data-model.md#indexes) in one change |
| **An OAuth *scope* failure reads exactly like a missing IAM *role*** | `403 … ACCESS_TOKEN_SCOPE_INSUFFICIENT` naming an IAM method, while the binding is present and correct. A client resolves ADC at the scope *it* needs, so a token borrowed from one API's client is rarely valid for another's | Any second Google API called with a client built for a different one — YouTube and Secret Manager at M4 |
| **Credentials differ in *kind* between local and Cloud Run** | Impersonated credentials can sign for themselves; the metadata server's cannot. Code that works locally raises in production | Anything signing, minting tokens, or impersonating — Cloud Tasks OIDC at M5 |
| **Model availability is per project *and* per location** | `NOT_FOUND` naming a model the design chose, with nothing before the first turn detecting it | Any model change, and `prod` when it exists |
| **A stored ADK event is `snake_case`** | `Event` declares camelCase aliases, but `append_event` stores `model_dump()` with the default `by_alias=False`. A reader that assumes the aliases silently finds nothing | M3's tool chips and M4's report events, both read from stored events. [02-data-model.md](02-data-model.md#sessions--events-adk-owned-layout) states the shape; `session-event-vectors.json` pins it |
| **A hand-written fixture can encode the same wrong assumption as the code** | Every test passes and the feature is broken | Anywhere a test fixture stands in for a shape this project does not define. Generate it instead — `gen_event_vectors.py`, `gen_ordering_vectors.py` |
| **Constructing a Google client resolves credentials** | Every such constructor calls `google.auth.default()`, so building one while assembling the app makes the app unimportable without credentials. Locally `dev.sh` exports `FIRESTORE_EMULATOR_HOST` (anonymous Firestore client) and a dev machine usually has ADC, so it passes; CI has neither. It was fixed once for Firestore and *recurred immediately* for the two GCS clients, which only a deployed `ENV` reaches | Any client built in a constructor. `coach.core.lazy` defers them; `tests/test_import_without_credentials.py` pins it for `local` **and** deployed settings, forcing the absence of credentials rather than relying on it |
| **A proxy is not an instance** | The fix for the row above, applied to the artifact service, broke every deployed turn: `Runner` puts it on an `InvocationContext`, a pydantic model that validates the field with `isinstance`, so `1 validation error … Input should be an instance of BaseArtifactService` — while locally the branch is never reached, because without `ARTIFACT_BUCKET` the in-memory service is real. The same proxy failed a second way in silence: it refuses underscore attributes, so `artifact_part_uri` lost `_get_blob_name` and recorded `artifact://` URIs the model cannot dereference | Any deferred object handed to a library rather than only called by us — M3's tools and M5's Cloud Tasks client. Defer with a **provider** (a callable resolved at first use) wherever the type is part of the surface: `integrations/artifacts.artifact_service_provider`, pinned by `tests/test_artifact_service_provider.py` |

---

## M3 — The coach acts on the board (~1.5 weeks)

- Domain tools (`add_task`, `split_task`, `set_task_state`, `set_next_up`, `reorder_task`,
  `update_project_prefs`, …) wrapping the service layer, with guards and confirmation
  gating for `discard_task`.
- Socratic project intake: creating a project opens an intake session that elicits goals
  and constraints before proposing a task list.
- `before_agent_callback` prompt assembly from project goal + effective prefs + board state.
- Task-splitting behaviour respecting the effective duration budget.
- Tool activity chips in the chat UI; `board_update` push → Query invalidation.

**Exit:** Playwright flows #1, #2, #7 pass; asking for a 4-hour task yields properly sized
subtasks with a correct parent rollup; project-level duration overrides are respected.

---

## Status after M3

Complete and deployed to `coach-dev`. What follows is the carry-over a later milestone
needs, recorded here because it is not derivable from the code.

**Met.** Golden flows #1, #2, and #7 pass on all four Playwright projects, alongside the
M1 and M2 specs: 25 specs, 100 runs. Asking for a four-hour task yields subtasks that each
fit the budget with a parent rollup equal to their sum, and a project with a two-hour
override sizes work differently from one on the 45-minute global default *in the same
browser, from the same sentence*. 384 backend tests, 194 web.

**Verified by hand on `coach-dev` on 2026-08-19**, against Vertex and a real model rather
than the stub — which is the only way to exercise the tool declarations ADK derives from
our Python signatures, since the stub never reads one. Four things were checked: the
coach's tool calls change the board; creating a project opens the intake conversation and
the coach proposes a task list into it; the board moves without a reload while the coach
works, and the chips are still in the transcript afterwards; and asking it to discard a
task produces the confirmation prompt, which goes through when answered. `terraform apply`
ran, so the `sessions.projectId` collection-group index M3 added is live — though the
fallback scan it backs only runs for a project created before `intakeSessionId` existed,
so the index is present rather than exercised.

**Unusually, the deploy found nothing.** Every prior milestone's first deployed run
surfaced a defect that a green local gate had not (M2's was the proxied artifact service).
M3's two post-gate defects came from *use* instead — tool chips vanishing when a turn
finished, and the board not refreshing on a tab that had opened a workspace first — and
both were fixed on this branch. That is a shift in where the remaining risk sits, not an
absence of it: both were client-side state bugs, which is the half of the system the
deploy step does not probe at all.

**Flow #7 is evidence rather than staging, and that is a property of how it is built.**
The stubbed model reads its task budget out of the *rendered system instruction* — the
line `agents/prompt.py` writes as `Default task length: 120 minutes` — so nothing in the
browser or in the stub knows which project it is in. Subtasks that follow a project's
override can therefore only have come from the prompt the server assembled, which is what
"project-level duration overrides are respected" actually means.
`tests/test_stub_model.py` owns both ends of that parse, so a change to the wording fails
there rather than as a Playwright timeout.

**Two defects were reported from use after the milestone was green**, both invisible to
the full local gate and both already rows in the table below:

- **Tool activity vanished when a turn finished.** Chips lived only in `useStreamStore`,
  which is cleared on `turn_complete`, and `toMessages` dropped every stored event that
  carried only a function call — so a reload or a revisit showed a conversation in which
  tasks had appeared by themselves. M2 deferred this with the note that a resumed client
  "rebuilds them from the finalized transcript"; nothing rebuilt them.
- **The board stopped refreshing on a tab that had opened a workspace first.**
  `getSocket(deps)` ignores its arguments after the first call, and React runs child
  effects before parent ones, so `TaskWorkspacePage` built the socket for its presence
  heartbeat before `AppShell` could hand over the `board_update` callback.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| `board_update` **across instances** | **M5** | `ws/hub.py` reaches the sockets on *this* process. For M3 that is the whole population: the tool runs inside the turn the user's own request started, and session affinity puts their socket there. A scheduled run executes wherever Cloud Tasks lands it, with no relation to where the owner is connected, so the ledger needs a cross-instance channel — Firestore, since one already exists |
| Autonomous mode's **reduced tool set** | **M5** | [03-agent-design.md](03-agent-design.md#safety-rails-on-autonomy) forbids `discard_task`, `update_learner_profile`, and `update_project_prefs` in background work. There is no autonomous agent yet, so there is nothing to reduce; `propose_tasks` builds its subset there, from the same `DomainTools` |
| `origin: "agent"` badging is on tasks, not on **runs** | **M5** | Every agent write records `origin`, which the board badges. `runId` and the per-run undo the "Updated by your coach" banner needs arrive with the ledger |
| `list_tasks` as the model's **only** board read | — | The prompt carries the board too, so the tool is a refresh rather than the source. Cheap, and it saves a tool round trip on the first turn; revisit if prompt size becomes the constraint |
| Everything still open after M2 | as recorded | Content scanning (M7), `subscribe` by `runId` (M5), `turns` composite indexes (M5), nightly evalsets (M4–M6), `prod` and `terraform destroy` |

**Endpoints in the API contract still unimplemented**: `POST /api/sessions/{sid}/research`
and everything under reports (M4), runs (M5), `PATCH /api/me/learner-profile`'s Settings
UI (M6), and `DELETE /api/me` (M7).

**Open questions Q1–Q3** ([10-risks.md](10-risks.md#open-questions)) were due at this
milestone and are settled, each taking its default. Q4 is due at M4.

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| `POST /api/projects/{id}/session` added | [04-api-contract.md](04-api-contract.md) has `POST /api/projects` *create* the intake session and nothing that resolves a project back to it. Every visit after the one that created it needs exactly that | `api/routers/projects.py` |
| `projects/{id}.intakeSessionId` added | The alternative is a collection-group scan of the project's sessions on every board load. The scan survives as the fallback for projects created before the pointer, and repairs it when it runs | [02-data-model.md](02-data-model.md#projectsprojectid) |
| The intake conversation lives **on the board screen** | [06-frontend.md](06-frontend.md#routes) gives the intake session no route of its own, and the board is the screen a new project lands on. Beside the board rather than on its own page is also what makes flow #1 legible — the learner watches cards appear as the coach proposes them | `pages/BoardPage.tsx` |
| Prompt context is injected as **`temp:` state** | [03-agent-design.md](03-agent-design.md#coach_agent) says "injected as state"; `temp:` is the lifetime that means. Session `state` is stored as a JSON *string*, so a plain key would re-serialize the whole board onto the session document on every appended event, and ADK trims `temp:` deltas before persistence. The cost is one contentless event per turn, which the transcript drops | `agents/prompt.py` |
| `add_task`'s per-run cap lives in `temp:` state too | The cap is "≤ 5/run". A persisted counter would make the sixth task of a *conversation* impossible rather than the sixth of a turn | `agents/context.py` |
| A failed tool **returns** a result rather than raising | An exception out of a tool aborts the invocation, so a model that asked for a nine-hour task would end the turn instead of being told the number is too big and trying again. Guards answer `{"ok": false, "error": …}`; anything that is not a `CoachError` still propagates | `agents/tools.py` |
| `update_project_prefs` takes named arguments, not a patch object | [03-agent-design.md](03-agent-design.md#domain-tools) says "whitelist of keys". Spelling the keys out as parameters *is* the whitelist; an open patch argument would let the model write fields it invented onto the project document | `agents/tools.py` |
| `discard_task` is gated with ADK's `require_confirmation`, and a turn may carry **only** a confirmation | The gate then holds whether or not the model cooperates: the call becomes `adk_request_confirmation` and the body runs on the answer. Answering is a turn with no text and no attachment, so `start`'s "a turn needs text, an attachment, or both" had to count a confirmation as content | `agents/tools.py`, `services/turns.py` |
| Tool activity is part of the **stored transcript**, not only of the live stream | The live buffer is cleared on `turn_complete`, so chips rendered from it alone last seconds. Calls and outcomes are *separate* stored events, paired by call id | `lib/transcript.ts`, `components/session/ToolChips.tsx` |
| A chip has **three** outcomes, not two | A refused guard is a result (`{"ok": false}`), and ADK's answer to a call awaiting confirmation is neither an `ok` nor an error — `null` renders as neither a tick nor a cross, because both would be a claim the transcript cannot support. `tool_result`'s `ok` was hard-coded `True` for the same reason and now reads the tool's result | `lib/transcript.ts`, `services/turns.py` |
| `board_update` is a **registration** on the socket, and a completed turn invalidates the board as well | The first because `getSocket`'s constructor arguments belong to whoever called it first. The second because frames are not checkpointed: a client whose socket was down while a tool ran resumes its *text* and never hears the board moved, and from M5 a run on another instance never sends it one. The push makes the board feel live; the invalidation makes it correct | `lib/socket.ts`, `components/session/SessionPane.tsx` |
| The agent may not complete or discard a task through `set_task_state` | [10-risks.md](10-risks.md#open-questions) Q1 was due at M3 and takes its default: completion is the learner's click. Enforced in the tool rather than asked for in the instruction, and `discarded` is refused on the same line because it would otherwise be a second route around `discard_task`'s confirmation gate | `agents/tools.py` |
| `Principal.source` gained `"agent"` | Nothing branches on it; it is the audit label. Calling a tool call an `id_token` request would be a lie in the one place a reader goes to find out who did something | `core/principal.py` |
| `APP_NAME` moved to `core/app.py` | It was in `agents/runner.py`, so `services/sessions.py` imported from `agents/` — an inversion of the layering that became visible, as an import cycle, only once `agents/` grew a module importing `services/` | `core/app.py` |
| The transcript pins itself to its own bottom by `scrollTop`, and the chat pane is height-bounded on mobile | `scrollIntoView` scrolls every scrollable ancestor including the document, so a streaming reply moved the *page* once per delta on a short viewport: the composer walked away under the reader's finger and cancel was unreachable while there was something to cancel. Found as a mobile-only e2e flake whose cause was neither mobile-specific nor timing | `components/session/Transcript.tsx`, `components/session/SessionPane.tsx` |
| Two M2 e2e specs were made deterministic rather than left intermittent | Flow #4 raced the reconnect banner — `backoffDelay(0)` plus a round trip, often shorter than one polling interval — and now holds the reconnect's ticket for 1.5 s. The cancel spec sends a long prompt and waits for text on screen, so it races neither the 202 nor the end of generation. Both pre-existing, both found only by running the suite many times in a row. [08-testing.md](08-testing.md#end-to-end-playwright) | `e2e/workspace.spec.ts` |
| The stubbed model emits **scripted tool calls**, planned from *this turn's* function responses | Flows #1, #2, and #7 need the coach to act. Scoping the plan to the turn is the loop's termination argument: asked of the whole session history, "have I already split something?" answers yes forever; asked of nothing, the stub re-issues `split_task` until the turn never ends | `integrations/stub_model.py` |

### Three more rows for the table above

M3 recurred two rows of [the M2 table](#what-a-green-local-run-does-not-prove) — the
composite collection-group query (avoided rather than hit, in `find_intake_session_id`)
and the shape of a stored ADK event — and added three of its own. M4 and M5 touch all
five.

| Trap | How it presents | Where it will recur |
| --- | --- | --- |
| **The event a UI reads is rarely the last one written** | ADK appends a function-response event *after* the confirmation request, so a reader keying on position finds nothing and renders no control. Silent: the transcript looks merely uneventful | M4's report events and M5's run-status events are both read out of a transcript that keeps growing after them. Key on identity — a call id, a report id — never on position |
| **A test that replays one fixture at a time cannot see a pairing** | A call and its outcome are two stored events. Feeding them separately proved the reader parsed each one and never that it joined them, so a chip with no result passed every assertion | Any two-event relationship: a report and its items, a run and its steps. Replay the vectors as **one** transcript, which `transcript.test.ts` now does |
| **A singleton's constructor arguments belong to whoever called it first** | `getSocket(deps)` ignores them afterwards, and React runs child effects before parent ones — so the *page* built the socket and the app shell's callback was dropped for the life of the tab. Every golden flow missed it by starting on the same screen | Anything configured at construction and reached for from more than one component. Prefer a registration; and vary an e2e's entry screen, because a suite that always starts in the same place tests one mount order |

---

## Interlude before M4 — web toolchain, composite tasks, rendered markdown

Not a milestone: three changes to `apps/web` taken between M3 and M4, on their own branch
and through the same gate. Recorded here because two of them change a convention the rest
of the repo is written against.

| Change | Decision | Where |
| --- | --- | --- |
| Prettier formats, ESLint lints | The two tools stop overlapping: `eslint-config-prettier` last in the config, no `eslint-plugin-prettier`, and the formatter runs *after* `--fix` in `dev.sh lint` so a fixer's rewrite is formatted in the same pass. Config matches the style already in the tree (no semicolons, single quotes, 96 columns), and `prettier-plugin-tailwindcss` sorts `className` | [07-infra-deploy.md](07-infra-deploy.md#formatting-and-linting) |
| Import order is a formatter plugin, not a lint rule | `@ianvs/prettier-plugin-sort-imports` enforces the grouping the tree already used (built-ins, third-party, `@/`, relative) and sorts specifiers. It is safe as a *formatter* only because it treats a side-effect import as a barrier and never moves imports across one — `import 'katex/dist/katex.min.css'` has to stay where it is. `prettier-plugin-tailwindcss` must stay last in the plugin list | `.prettierrc.json` |
| Prettier's remit stops at `apps/web` | `docs/` and the root markdown are hand-wrapped prose with tables aligned for terminal reading. Reflowing eleven design documents is a diff nobody can review | `.prettierignore` |
| A composite task shows its subtasks in the workspace | `GET /api/tasks/{id}` already returned `subtasks[]`, so the board could expand a parent and the task's own screen could not — the one screen dedicated to that task knew least about it | `pages/TaskWorkspacePage.tsx` |
| A subtask still has no route | The parent's session is where subtasks get worked through; four transcripts each holding a quarter of one piece of work is worse than one holding all of it. The cards carry state actions and navigate nowhere | [06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid) |
| The transcript renders markdown, assembled per capability | `react-markdown` + `remark-gfm` + `remark-math`/`rehype-katex`, with `shiki` and `mermaid` behind dynamic imports. One plugin per capability rather than an all-in-one component, so each can be pinned, reasoned about, and replaced alone | [06-frontend.md](06-frontend.md#markdown-in-the-transcript) |
| Raw HTML stays off, permanently | The transcript renders model output, some of it quoted from fetched pages. No `rehype-raw`; the only `dangerouslySetInnerHTML` in the app is mermaid's own SVG, which mermaid sanitizes at `securityLevel: 'strict'` | `components/markdown/` |
| Mermaid renders only after `turn_complete` | Half a table is a table; half a graph is a parse error, and an error box flashing for the two seconds a definition streams in is worse than a diagram arriving a moment late. It also keeps mermaid out of the streaming path | `components/markdown/CodeBlock.tsx` |
| Highlighting is dual-theme output, not a runtime restyle | Shiki emits `--shiki-light` and `--shiki-dark` per token and a rule gated on `.dark` picks; the theme switch stays one class on `<html>` and nothing re-highlights. Mermaid cannot do this — its SVG is baked — so a diagram is the one thing that re-renders on a theme change | `index.css`, `components/markdown/Mermaid.tsx` |
| Only the coach's messages are markdown | The learner's message is the record of what they sent. Rendering it would collapse their line breaks, reflow a pasted stack trace, and emphasise a literal `*` | `components/session/Transcript.tsx` |
| Tokens, not HTML, from the highlighter | `codeToHtml` returns a string only `dangerouslySetInnerHTML` can render, and the transcript's whole rule is that model text never becomes markup. `codeToTokens` returns data. The one exception in the app is mermaid's own SVG, which it constructs and sanitizes itself | `lib/highlighter.ts` |
| Shiki runs on the JavaScript regex engine | The default oniguruma engine's WebAssembly is most of the download and the only part a strict CSP would object to. Passing an engine also keeps `shiki/wasm` from ever being fetched, though the build still emits the chunk | `lib/highlighter.ts` |
| The stub model answers one prompt in markdown | `Markdown.test.tsx` has to mock shiki and mermaid, so nothing local proves those `import()` chunks resolve in a *built* bundle — the failure mode docs/09 keeps a table of. `show me the formatting` gives the e2e something to render against the real image | `integrations/stub_model.py`, `e2e/markdown.spec.ts` |

---

## M4 — Research (~1.5 weeks)

- `search_agent` (grounded search) + `research_agent`; `fetch_url` with SSRF guards;
  `youtube_find_by_duration` with real duration filtering and a 24 h cache.
- `ResearchReport` schema, `post_research_report` tool with budget validation, storage, and
  the session event.
- Report UI: separated required/optional blocks, budget meter, citations, per-item completion.
- `POST /api/sessions/{sid}/research` — the manual trigger, on the shared run path.

**Exit:** Playwright flow #5 passes; report validation rejects over-budget required lists;
recommended videos actually fit the remaining budget (verified against real API responses
in the nightly live run).

---

## M5 — Autonomy (~1.5 weeks)

- Cloud Scheduler → `/internal/tick`; Cloud Tasks queue → `/internal/runs/{id}/execute`;
  OIDC verification on both.
- `autonomous_runs` ledger with per-step checkpointing, resume-at-cursor, retry policy,
  and step-level idempotency.
- Project agent lease; presence tracking and the double-checked owner-present guard.
- `autonomous_workflow` SequentialAgent with the reduced tool set; `select_next_task` and
  `reprioritize` as deterministic code steps.
- "Updated by your coach" banner with per-run undo; postponement sweep.
- Local `dev.sh tick` path with the in-process `JobQueue`.

**Exit:** Playwright flows #6 and #8 pass; killing the process mid-run and re-ticking
resumes without re-running the research step; a project with its owner online is never
touched.

---

## M6 — Learner model and adaptation (~1 week)

- `CoachMemoryService` + contract suite; `load_memory` wired into the coach.
- Session-close summarization into memory; `update_learner_profile` typed tool with
  versioning and an audit trail.
- Prompt construction consumes the profile; guidance style and verbosity visibly change
  behaviour.
- "What your coach knows about you" settings screen with per-field edit and reset.

**Exit:** across three sessions the coach demonstrably adapts (evalset check); every
profile change is attributable to a session and reversible by the user.

---

## M7 — Hardening and launch readiness (~1.5 weeks)

- Rate limits, per-user daily token and run quotas, `usage/*` counters.
- Observability: dashboards, alert policies, log-based metrics, trace sampling.
- Error handling pass: retryable vs terminal, user-facing messages, empty states.
- Account deletion cascade; data export.
- Accessibility audit and mobile layout pass, both run **in light and dark** — contrast,
  focus rings, and the required/optional report distinction checked in each.
- Load test (50 concurrent turns); cost model validated against real usage.
- Security review: SSRF, upload handling, ticket lifecycle, OIDC verification, IAM scope,
  confirmation that `datastore.user` is held only by `coach-api-sa` and that the `ENV=local`
  auth bypass is unreachable in prod, dependency audit.

**Exit:** dashboards show a full day of synthetic traffic within cost and latency budgets;
security review closed; runbook written.

---

## Post-v1 backlog

Billing (Stripe, plan tiers, metering on the existing `usage/*` counters) · email digests
of autonomous updates · vector memory via Firestore `find_nearest` · project sharing and
collaboration · calendar integration for scheduling study sessions · spaced-repetition
review tasks · an "explain why you recommended this" trace view · Gemini Live for voice
sessions · mobile PWA with offline board reads.

## Critical path

`M0 → M1 → M2` is strictly sequential and carries most of the risk. `M3` and `M4` can
overlap once M2 lands (different tool surfaces, shared runner). `M5` depends on M4 for the
research workflow it schedules. `M6` is independent of M4/M5 and can slot in wherever
convenient. Roughly 11 weeks of sequential work, ~9 with the M3/M4 overlap.
