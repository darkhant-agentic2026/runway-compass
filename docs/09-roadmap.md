# Roadmap

Ten milestones. Each has a demoable outcome and explicit exit criteria. Sizes assume one
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
  M9 because the inline script and `color-scheme` handling are cheap now and awkward to
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
| Nightly evalsets, live-API tests, real-auth test | M4–M7 | Specified in [08-testing.md](08-testing.md#ci-wiring), none implemented |
| `prod` environment | before any release | No GCP project, no GitHub Environment. Note that environment protection rules need a paid plan on private repos ([RUNBOOK](../infra/terraform/RUNBOOK.md)) |
| `terraform destroy` / from-scratch reproducibility | before relying on it | M0's other exit criterion, never exercised. `google_identity_platform_config` likely cannot be deleted, so a re-apply would need the import again |

**Endpoints in the API contract that are not implemented yet**, all by milestone rather
than oversight: the intake session created by `POST /api/projects` (M2), everything under
sessions, turns, uploads, and runs (M2–M5), `PATCH /api/me/learner-profile`'s Settings UI
(M7), and `DELETE /api/me` (M9).

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
| Content **scanning** on `POST /api/uploads/{id}/finalize` | **M9** | The contract lists it in that step and nothing scans. An accepted MIME type and a size cap are the only checks on uploaded bytes |
| `subscribe` by `runId` | **M5** | The frame is accepted and answered with an explicit error until the run ledger exists |
| Tool-activity chips on **resume** | M3+ | Chips render from the live stream but are not checkpointed, so a resumed client rebuilds them from the finalized transcript rather than the stream |
| Composite indexes for the `turns` queries | **M5** | `list_running_for_instance` and `expire_stale` were written ahead of a caller, needed indexes that do not exist, and were deleted. The ledger sweep should add each query *with* its index and its row in the index table |
| Nightly evalsets, live-API tests, real-auth test | M4–M7 | Still as recorded after M1 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1. `envs/prod` also has a commented `vertex_location` needing its own decision |

**Endpoints in the API contract still unimplemented**, by milestone rather than oversight:
`POST /api/sessions/{sid}/research` and everything under reports (M4), runs (M5),
`PATCH /api/me/learner-profile`'s Settings UI (M7), and `DELETE /api/me` (M9).

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
| Everything still open after M2 | as recorded | Content scanning (M9), `subscribe` by `runId` (M5), `turns` composite indexes (M5), nightly evalsets (M4–M7), `prod` and `terraform destroy` |

**Endpoints in the API contract still unimplemented**: `POST /api/sessions/{sid}/research`
and everything under reports (M4), runs (M5), `PATCH /api/me/learner-profile`'s Settings
UI (M7), and `DELETE /api/me` (M9).

**Open questions Q1–Q3** ([10-risks.md](10-risks.md#open-questions)) were due at this
milestone and are settled, each taking its default. Q4 is due at M4.

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| `POST /api/projects/{id}/session` added | [04-api-contract.md](04-api-contract.md) has `POST /api/projects` *create* the intake session and nothing that resolves a project back to it. Every visit after the one that created it needs exactly that | `api/routers/projects.py` |
| `projects/{id}.intakeSessionId` added | The alternative is a collection-group scan of the project's sessions on every board load. The scan survives as the fallback for projects created before the pointer, and repairs it when it runs | [02-data-model.md](02-data-model.md#projectsprojectid) |
| The intake conversation lives **on the board screen** | [06-frontend.md](06-frontend.md#routes) gives the intake session no route of its own, and the board is the screen a new project lands on. Beside the board rather than on its own page is also what makes flow #1 legible — the learner watches cards appear as the coach proposes them | `pages/BoardPage.tsx` |
| Prompt context is injected as **`temp:` state** | [03-agent-design.md](03-agent-design.md#project_coach-and-task_teacher) says "injected as state"; `temp:` is the lifetime that means. Session `state` is stored as a JSON *string*, so a plain key would re-serialize the whole board onto the session document on every appended event, and ADK trims `temp:` deltas before persistence. The cost is one contentless event per turn, which the transcript drops | `agents/prompt.py` |
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
| The stubbed model emits **scripted tool calls**, planned from *this turn's* function responses | Flows #1, #2, and #7 need the coach to act. Scoping the plan to the turn is the loop's termination argument: asked of the whole session history, "have I already added a subtask?" answers yes forever; asked of nothing, the stub re-issues the call until the turn never ends | `integrations/stub_model.py` |

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
| Prettier formats, ESLint lints | The two tools stop overlapping: `eslint-config-prettier` last in the config, no `eslint-plugin-prettier`, and the formatter runs *after* `--fix` in `dev.sh lint` so a fixer's rewrite is formatted in the same pass. Config keeps the style already in the tree (single quotes, 96 columns) but turns semicolons **on**, which the tree did not use — ASI makes the end of a statement depend on how the next line starts. `tabWidth` and `endOfLine` are written out at their current defaults rather than inherited, since a formatter's defaults moving in a major version is a whole-repo diff. `prettier-plugin-tailwindcss` sorts `className` | [07-infra-deploy.md](07-infra-deploy.md#formatting-and-linting) |
| Import order is a formatter plugin, not a lint rule | `@ianvs/prettier-plugin-sort-imports` enforces the grouping the tree already used (built-ins, third-party, `@/`, relative) and sorts specifiers. It is safe as a *formatter* only because it treats a side-effect import as a barrier and never moves imports across one — `import 'katex/dist/katex.min.css'` has to stay where it is. `prettier-plugin-tailwindcss` must stay last in the plugin list | `.prettierrc.json` |
| Prettier's remit stops at `apps/web` | `docs/` and the root markdown are hand-wrapped prose with tables aligned for terminal reading. Reflowing eleven design documents is a diff nobody can review | `.prettierignore` |
| A composite task shows its subtasks in the workspace | `GET /api/tasks/{id}` already returned `subtasks[]`, so the board could expand a parent and the task's own screen could not — the one screen dedicated to that task knew least about it | `pages/TaskWorkspacePage.tsx` |
| A subtask still has no route | The parent's session is where subtasks get worked through; four transcripts each holding a quarter of one piece of work is worse than one holding all of it. The cards carry state actions and navigate nowhere | [06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid) |
| The transcript renders markdown, assembled per capability | `react-markdown` + `remark-gfm` + `remark-math`/`rehype-katex`, with `shiki` and `mermaid` behind dynamic imports. One plugin per capability rather than an all-in-one component, so each can be pinned, reasoned about, and replaced alone | [06-frontend.md](06-frontend.md#markdown-in-the-transcript) |
| Raw HTML stays off, permanently | The transcript renders model output, some of it quoted from fetched pages. No `rehype-raw`; the only `dangerouslySetInnerHTML` in the app is mermaid's own SVG, which mermaid sanitizes at `securityLevel: 'strict'` | `components/markdown/` |
| Mermaid renders only after `turn_complete` | Half a table is a table; half a graph is a parse error, and an error box flashing for the two seconds a definition streams in is worse than a diagram arriving a moment late. It also keeps mermaid out of the streaming path | `components/markdown/CodeBlock.tsx` |
| Highlighting is dual-theme output, not a runtime restyle | Shiki emits `--shiki-light` and `--shiki-dark` per token and a rule gated on `.dark` picks; the theme switch stays one class on `<html>` and nothing re-highlights. Mermaid cannot do this — its SVG is baked — so a diagram is the one thing that re-renders on a theme change | `index.css`, `components/markdown/Mermaid.tsx` |
| ~~Only the coach's messages are markdown~~ — **reversed after M4** | The original reason was that the learner's message is the record of what they sent, and rendering it would collapse their line breaks, reflow a pasted stack trace, and emphasise a literal `*`. Two of the three are now answered rather than accepted: `remark-breaks` keeps the line breaks for their messages only, and every message carries a copy control yielding the *source*, so the exact thing they sent is a click away. The literal `*` remains a real cost and is the smaller one | `components/session/Transcript.tsx` |
| Tokens, not HTML, from the highlighter | `codeToHtml` returns a string only `dangerouslySetInnerHTML` can render, and the transcript's whole rule is that model text never becomes markup. `codeToTokens` returns data. The one exception in the app is mermaid's own SVG, which it constructs and sanitizes itself | `lib/highlighter.ts` |
| Shiki runs on the JavaScript regex engine | The default oniguruma engine's WebAssembly is most of the download and the only part a strict CSP would object to. Passing an engine also keeps `shiki/wasm` from ever being fetched, though the build still emits the chunk | `lib/highlighter.ts` |
| M1's reorder spec waits for the server before reloading | It failed once on webkit in nine runs and never again. The window is real rather than slow: a navigation cancels in-flight requests, so reloading before the `POST /reorder` lands stops it reaching the server at all, and the reloaded board then honestly shows the original order. An optimistic mutation makes that window invisible from the UI, which is what let a pre-existing race sit in a spec since M1 | `e2e/board.spec.ts` |
| The stub model answers one prompt in markdown | `Markdown.test.tsx` has to mock shiki and mermaid, so nothing local proves those `import()` chunks resolve in a *built* bundle — the failure mode docs/09 keeps a table of. `show me the formatting` gives the e2e something to render against the real image | `integrations/stub_model.py`, `e2e/markdown.spec.ts` |

---

## M4 — Research (~1.5 weeks)

- `search_agent` (grounded search) + `research_agent`; `fetch_url` with SSRF guards;
  `youtube_find_by_duration` with real duration filtering and a 24 h cache.
- `ResearchReport` schema, `post_research_report` tool with budget validation, storage, and
  the session event.
- Report UI: separated checklist/optional blocks, budget meter, citations, per-item completion.
- `POST /api/sessions/{sid}/research` — the manual trigger, on the shared run path.
- **The task-model amendment** decided at the start of the milestone: `draft` as the state
  every task starts in, `in_progress` replacing a singular `current`, and an ordered
  checklist on every leaf task that research populates and whose completion completes the
  task ([02-data-model.md](02-data-model.md#task-items)). It lands here rather than on its
  own branch because the checklist's only writer is the research run.

**Exit:** Playwright flow #5 passes; report validation rejects over-budget required lists;
recommended videos actually fit the remaining budget (verified against real API responses
in the nightly live run); a finished checklist completes its task without the learner
touching a state control.

---

## Status after M4

Complete and deployed to `coach-dev`. What follows is the carry-over a later milestone
needs, recorded here because it is not derivable from the code.

**The deploy found two defects, and both were in the half of the system a local run cannot
reach**: the YouTube API key was the Terraform placeholder rather than a key, and the tool
that discovered this told only the model. They are the last two rows of the table below.
Every research report on the first deployed revisions therefore contained no videos, and
the service looked entirely healthy while it happened.

**Met.** Golden flow #5 passes on all four Playwright projects, alongside every earlier
spec: 31 specs, 124 runs, three consecutive green suites. Report validation rejects an
over-budget required list, an item in both lists, and a required item with no `why`, each
with its own test. Recommended videos fit the remaining budget because the filter is
arithmetic on `contentDetails.duration` rather than a request to the model — asserted
against recorded `search.list` / `videos.list` payloads; the nightly live run against the
real API is still unimplemented, and is in the table below. 487 backend tests, 229 web.

**M4 also carried a data-model change that was not in the original plan**, decided at the
start of the milestone and recorded in the design documents rather than only here: the task
state machine gained `draft`, `current` became `in_progress` and stopped being singular, and
a leaf task gained an ordered checklist that research populates and that completes the task
when it is finished. [02-data-model.md](02-data-model.md#task-items) is the specification;
the decisions table below records what implementing it settled.

**Open questions Q4–Q6** ([10-risks.md](10-risks.md#open-questions)) are settled, each
taking its default. Q4 was due here; Q5 and Q6 were due at M5 and were answered early,
because M4 builds the path M5 schedules and leaving the cadence open would have meant
parameterising guards on an undecided number.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| **Seed `youtube-api-key`** (RUNBOOK §4) and re-verify videos | **now** | `terraform apply` ran; the secret still holds the placeholder, which is why no report has recommended a video. The service now says so at `ERROR` on startup instead of degrading in silence |
| Re-run the **hand verification** for videos specifically | **now** | The rest of M4 was verified on `coach-dev`; the YouTube path never succeeded there, so nothing has yet exercised `search.list` → `videos.list` against the real API |
| Nightly **live** YouTube and search tests | M7 | The recorded-fixture half is done; `--live` is not wired. What no fixture can catch is a quota shape or a response field moving |
| ADK **evalsets** for research quality | M7 | The tool contract is tested; whether a report is *good* is not, and cannot be by a stub. Still as recorded after M1 |
| `subscribe` by `runId` | **M5** | Still deferred. A manual run has a `turnId` and the client watches that; a *scheduled* run has no turn, which is when the frame becomes necessary |
| `propose_tasks` and `reprioritize` | **M5** | `research_agent` has no board-mutating tools by design ([10-risks.md](10-risks.md#r7--prompt-injection-via-fetched-pages-and-uploads)); reshaping the board is a separate step with its own budget |
| `post_report`'s **event-level idempotency** | **M5** | [05-autonomous-runs.md](05-autonomous-runs.md#execution-semantics) already flags this. The report document is idempotent by overwrite; the session event is not, and a manual run cannot be re-executed so nothing exercises it yet |
| Everything still open after M3 | as recorded | Content scanning (M9), `turns` composite indexes (M5), `board_update` across instances (M5), `prod` and `terraform destroy` |

**Endpoints in the API contract still unimplemented**: everything under runs (M5),
`PATCH /api/me/learner-profile`'s Settings UI (M7), and `DELETE /api/me` (M9). `GET
/api/runs/{runId}` is *reachable* through `ResearchService.get` but has no route, because
the contract files it under M5 and a manual run is watched by its turn.

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| `set_next_up` **reorders** rather than setting a state | It used to promote a task to `current`, which was singular and therefore *was* the pointer. With `nextUpTaskId` derived, pinning something is a reorder — and, unlike the old behaviour, it no longer silently un-starts whatever the learner had open | `agents/tools.py` |
| An explicit state write is **exempt from the derivation for its own transaction** | Otherwise invariant 6 and the `reopen` action fight: reopening a task whose checklist is fully ticked lands on `not_started`, is re-derived to `completed` in the same transaction, and the button does nothing at all — visibly, repeatably, with no error | `services/tasks.py` |
| `TaskService.set_research` goes through the board transaction | `researchStatus` is half of invariant 6, so the write that sets `done` is exactly the write that can complete a task. A bare field patch would leave it finished-but-open until something unrelated touched it | `services/tasks.py` |
| A report item's `why` becomes the checklist entry's one line | A checklist reads as things to do; a list of titles reads as a bibliography and leaves the learner to work out what each one is for. It is also why `why` is *required* on a required item and validated server-side | `services/reports.py` |
| `guided` defaults from `kind`, and the model may override | An exercise is worked through in conversation; an article is not. A report that says nothing about guidance still produces a sensible checklist | `services/models.py` |
| `fetch_url` extracts text with `html.parser`, not a markdown converter | [03-agent-design.md](03-agent-design.md#integration-tools) says "HTML→markdown", but the model is answering "does this page cover the task" and heading syntax does not help with that — while a dependency that parses hostile HTML is a real surface | `integrations/fetch_url.py` |
| Redirects are followed **by hand**, and every hop re-checked | `follow_redirects=True` validates the first URL and then connects wherever the server points. A public host answering `302 → 169.254.169.254` is the attack that works against a fetcher which only checks the first hop | `integrations/fetch_url.py` |
| The YouTube cache is **process-local** | It caches a quota cost, not a correctness property. A second instance paying for its own first lookup is cheaper than a collection, a TTL policy, and a read on the research path | `integrations/youtube.py` |
| A research run is an **ordinary turn** | It gets the same `turns/{turnId}` document, detached task, checkpoints, and broker subscription — so the disconnect guarantee, the tool chips, and resume all cover it without a second implementation. One argument to `TurnService.start` selects the agent | `services/research.py` |
| `TurnService.start` grew an `on_finished` **done callback** | Never an await — the module's first rule. `ResearchService` uses it to close the ledger and drop the lease the instant generation stops. See the new table below for what the polling version cost | `services/turns.py` |
| A manual run's ledger row omits the steps M4 does not implement | Absent rather than `pending`, so `cursor` — "first non-complete step" — stays truthful and M5's executor does not inherit a backlog of runs it thinks it left half-finished | `services/models.py` |
| `TaskRepository.find_current` deleted | It existed so `TaskService` could observe a duplicated `current` in order to repair it. There is no violation left to observe and no caller — the same reasoning that deleted M2's uncalled `turns` queries | `repositories/tasks.py` |
| `PATCH /api/reports/{rid}/items/{iid}` **refuses** `completed` | The field used to be accepted there. A client that has not caught up must fail loudly rather than write nothing and report success | `api/schemas.py` |

### Five more rows for the table above

M4 recurred one row of [the M3 table](#three-more-rows-for-the-table-above) — a singleton's
configuration belonging to whoever reached for it first, this time as a *query key* nothing
invalidated — and added four of its own. Two were found by golden flow #5 and neither was
visible to a fully green unit run. **The other two were found by deploying, and are the
first entries in this project's tables that a local run could not have caught even in
principle**: one is a value that only exists in a Terraform-provisioned environment, and
the other is an absence of logging, which no assertion was looking for.

| Trap | How it presents | Where it will recur |
| --- | --- | --- |
| **A push reaches the screens that were listening when it was written, not the ones that exist now** | `board_update` invalidated `['tasks', projectId]` and `['project', projectId]`; the task workspace reads `['task', taskId]`, which neither prefix covers. Nothing noticed for a milestone, because every writer until M4 was a tool the user had just talked to and the reply's own refetch hid it. A research run posts its report and says nothing further — so the checklist never appeared, on a screen where every unit test passed | Any new screen keyed differently from the one a push was designed for. M5's run banner and undo are both read from keys that do not exist yet. **When adding a writer, enumerate the readers** — and note that the *frame already carried* `taskIds`, so the information was there and unused |
| **An unseeded secret is a *value*, not an absence** | `terraform apply` seeds every Secret Manager secret with `REPLACE_ME_VIA_GCLOUD_SEE_RUNBOOK` and leaves the real one to RUNBOOK §4, a human step nothing fails without. The placeholder is a non-empty string, so `YouTubeClient` considered itself configured, sent it to Google, and turned the `400 API key not valid` into "the YouTube API did not answer". Every research report came back with no videos, on a deployment where `terraform apply` had succeeded and the service was otherwise healthy | Every secret this project adds, and the two that already exist beside this one. A reader that does not know the sentinel cannot distinguish "unset" from "set to something wrong" — and the sentinel exists precisely to make that distinction. `Settings` nulls it; `tests/test_config.py` pins the literal against the Terraform that writes it, because neither file can import the other |
| **A degraded integration that only tells the model is not telling anyone** | `youtube_find_by_duration` answered `{"ok": false, …, "Recommend written material instead"}` and logged nothing. The model complied, the report was well-formed, the turn succeeded, and the *only* evidence was an absence — no videos, ever. Nothing in Cloud Logging, nothing in the UI, nothing on `/readyz` | Any tool that degrades rather than failing. The return value is for the model; a `logger.warning` is for the operator, and a tool needs both. Distinguish the reasons, too: "the key is wrong" and "nothing is short enough" produce the same empty report and want opposite fixes |
| **A buffer with no exit stays in front of everything after it** | `useStreamStore.clear` is called from the `turn_complete` handoff and nowhere else, so a turn ending in `turn_error` was a permanent resident — and the pane read `Object.values(turns).find(…)`, the *first-inserted* match. After a 429 from Vertex the red error stayed on screen and the next reply streamed into a buffer nothing rendered. It had generated: a reload showed it, because a reload is what empties the store | Any keyed buffer whose removal sits on one path and whose read is a `find`. Two questions to ask of a store: *what removes an entry on every terminal path*, and *when there are several, which one does the UI mean*. Answering the second with insertion order is answering it by accident |
| **A lease outliving its run is a button the server refuses** | `post_research_report` pushes `board_update` — so the report renders — while the turn is still streaming its closing prose and the project's agent lease is still held. A poller closing the ledger half a second later left a window in which "Research again" answered `409 your coach is already working on this project`. It failed on two of four Playwright projects, roughly one run in three, and read as a timing flake | Anything whose *visible* completion precedes its *bookkeeping* completion. M5's scheduled runs have the same shape with a longer tail. Hang the bookkeeping off the work's own completion — `TurnService.start`'s `on_finished` — rather than off a clock |

**A third thing worth recording, which is a test-design trap rather than a defect.** Flow
#5's re-run spec failed intermittently in the full suite and never in isolation, because a
single `toBeVisible({ timeout: 30_000 })` sat inside Playwright's 30-second *test* timeout:
an assertion given the whole budget can never actually wait for it, so it fails at whatever
is left over and reads as a product bug. The fix was to raise the describe-block's timeout
and put the per-assertion waits back to the default. **A per-assertion timeout that equals
or exceeds the test timeout is always wrong**, and the symptom is a flake that points at
the feature rather than at the budget.

---

## M5 — Autonomy (~1.5 weeks)

- Cloud Scheduler → `/internal/tick`; Cloud Tasks queue → `/internal/runs/{id}/execute`;
  OIDC verification on both.
- `autonomous_runs` ledger with per-step checkpointing, resume-at-cursor, retry policy,
  and step-level idempotency.
- Project agent lease; presence tracking and the double-checked owner-present guard.
- `autonomous_workflow` SequentialAgent with the reduced tool set; `select_next_task` and
  `reprioritize` as deterministic code steps.
- **Requested research**: `POST`/`DELETE /api/tasks/{id}/research-request`, the
  `researchStatus: "pending"` + `researchRequestedAt` pair, the priority queue at the front
  of the tick, and the "Have my coach prepare this" / "Starts soon — cancel" control beside
  the existing inline trigger. Decided at the start of the milestone; see below.
- "Updated by your coach" banner with per-run undo; postponement sweep.
- Local `dev.sh tick` path with the in-process `JobQueue`.

**Exit:** Playwright flows #6 and #8 pass; killing the process mid-run and re-ticking
resumes without re-running the research step; **a project with its owner online is never
touched by auto-scheduled research, while research the learner queued by hand runs anyway
and runs first**.

### The presence guard applies to auto-scheduled work only — decided at the start of M5

The original exit criterion was "a project with its owner online is never touched", and
that is now too strong in one direction and too weak in another. The two kinds of
background work it lumped together are split by
[05-autonomous-runs.md](05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them):

- **Auto-scheduled** research — the work the coach signed up for when it created a task
  with `needsResearch: true` — is what the guard is for. Reshaping the board under a
  learner who is reading it is the failure, and being present is a good proxy for it.
- **Requested** research — the learner pressed a button — runs regardless of presence, and
  jumps the queue ahead of every auto-scheduled candidate. The guard would otherwise refuse
  the one case where intent is explicit: a learner presses "prepare this", stays on the
  page because they want to see it happen, and their being there is exactly what stops it.

A requested run also skips the 6-hour cooldown, `autonomousEnabled`, and quiet hours, for
the same reason in each case — all three are defaults about *unprompted* work. It still
takes the project lease and still counts against the daily quota, which are mutual
exclusion and a cost ceiling rather than policy about who asked.

**The queued path is where research is going.** The intent is for headless execution to
become the default and the inline `POST /api/sessions/{sid}/research` button to retire,
once the autonomous research agent has been shown to be robust running unattended. That
proof is deliberately postponed until the core M5 functionality is in place, so both paths
ship in this milestone and the inline one stays the primary action. Anything built here
should assume the queued path outlives the inline one.

---

## Status after M5

What follows is the carry-over a later milestone needs, recorded here because it is not
derivable from the code. **Complete and deployed to `coach-dev`.**

**The hand verification found two defects, both in the half of the system a local run
cannot reach at all — Cloud Scheduler driving real Cloud Tasks against a real model —
and both are now fixed, tested, and re-verified deployed.** `_recover` patched a run's
ledger row to `pending` *before* calling `enqueue_run`; when the enqueue then threw —
Cloud Tasks' own dedup, because the task name was keyed on `run_id` alone and collided
with the run's own previous attempt for up to an hour — that patch had already
committed, and the row was an orphan neither recovery query would ever find again.
Fixed by keying the task name on `run_id` **and** `attempts`, and by reverting the
ledger patch if the enqueue that was supposed to follow it fails. The second was only
reachable once the first was fixed: a **requested** run against a task the coach itself
had marked `needsResearch: false` (a `propose_tasks`-authored subtask) skipped research
and failed at `post_report`, identically on all three attempts, because `_research`
did not except `trigger: "requested"` from the `needsResearch` skip the way
`select_next_task` already does. Both are rows in the trap table below.

**Met locally and confirmed deployed.** Golden flows #6, #8, and #9 pass on all four
Playwright projects alongside every earlier spec. Killing a run mid-flight and
re-ticking resumes without re-running the research step — asserted by *counting model
invocations* across the two executions rather than by reading the ledger, because a
ledger that says `research` is complete while the agent ran twice would pass the weaker
assertion and fail the bill. Auto-scheduled research is skipped while the owner is
present; research the learner queued runs anyway and runs first. On `coach-dev`,
against a real Cloud Scheduler tick, Cloud Tasks delivery, and Gemini: autonomous
research completes end to end and a recommended YouTube video lands in a task's
checklist — closing the one row M4 left unconfirmed (RUNBOOK §4's `youtube-api-key` is
now seeded). A production run's `propose_tasks` step has also been observed creating a
task (`origin: "agent"` on the task document), which is the first end-to-end evidence
for the row below about the stub not exercising that tool path — the *stub* limitation
for the local suite still stands, and still wants the M7 evalset.

**The requested-research change, decided at the start of the milestone**, is specified in
[05-autonomous-runs.md](05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them)
and argued [above](#the-presence-guard-applies-to-auto-scheduled-work-only--decided-at-the-start-of-m5).
The queued path is the one intended to outlive the inline button.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| `propose_tasks`'s **tool path** under the stub | M7 | Locally, the stub answers the propose prompt in prose, so `test_autonomous_tools.py`'s enumeration is still the only local coverage of that tool. Deployed, against a real model, the path has now been observed working (see above); what M7's evalset adds is *quality*, not existence |
| Nightly evalsets, live-API tests, real-auth test | M7 | Still as recorded after M1 |
| Content **scanning** on finalize | M9 | Still as recorded after M2 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1 |

**Endpoints in the API contract still unimplemented**: `PATCH /api/me/learner-profile`'s
Settings UI (M7) and `DELETE /api/me` (M9). Everything under runs landed here.

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| A run step waits on an `asyncio.Event`, never on the generation task | `TurnService.start` must not grow an `await` on generation — that is the disconnect guarantee stated as an absence. The executor passes an `on_finished` callback that sets an event and waits for *that*, so a Cloud Tasks delivery that times out leaves inference running rather than killing it | `services/executor.py` |
| The run id reaches `post_research_report` as `state_delta`, not through the prompt callback | ADK applies `state_delta` to the user event *before* the root node runs, so `agents/prompt.py` must never give `RUN_ID_KEY` a default — its `state.update(defaults)` would erase it, and the symptom would be a duplicate report after a retry | `agents/context.py`, `services/turns.py` |
| `propose_tasks` is a **separate agent**, not the coach with a different message | The reduced tool set is the safety rail, and a rail expressed as an instruction is an honour system. It also drops every confirmation-gated tool for an independent reason: there is nobody to answer | `agents/autonomous_agent.py`, `agents/tools.py` |
| The autonomous tool set is built by **enumerating what is allowed** | A subtractive list silently re-admits every tool added afterwards, so the next destructive tool would be autonomous by default and nothing would report it | `agents/tools.py` |
| `board_update` crosses instances by **polling a per-user document** | `on_snapshot` is sync-only, exactly as for the M2 resume path. One point read per connected user every 3 s, and each frame carries the instance that wrote it so the publisher's own poller skips it | `repositories/board_events.py`, `ws/hub.py` |
| Undo **discards** a created task rather than deleting it | Discarding is the board's own word for "not work I am doing", it is reversible from the UI, and it keeps any conversation about that task reachable | `services/runs.py` |
| `TaskService.restore_order` writes an exact fractional key | `reorder` takes a *neighbour* and computes a key between two others. A recomputed key lands the task in the right position relative to the board as it is *now*, which is not where it was | `services/tasks.py` |
| `start == end` is an empty quiet-hours window | The only way to turn quiet hours off — there is no separate flag — and the way a test asks for a scheduler that does not depend on the hour it is run | `services/scheduler.py` |
| A tick that recovers a run excludes that project from scheduling | A crashed run's lease has expired *by definition*, so the lease guard reads the project as idle and the tick would queue a second run to race the first. Invariant 1 is "interrupted work is finished **before** new work is started" | `services/scheduler.py` |
| The quota is counted when a run is **created**, not when it succeeds | A failed run has still spent the model calls the quota exists to bound, and a counter of successes would let a broken project retry all day | `repositories/usage.py` |
| `PATCH /api/me/prefs` re-validates the merged prefs | `model_copy(update=…)` assigns without validating, so a nested patch left a plain `dict` where `QuietHours` belongs. Right in Firestore, wrong in the reply | `services/users.py` |
| `TASKS_TARGET_URL` is the **service** URL, not a path under it | It is also the OIDC audience both internal endpoints verify, and Cloud Scheduler mints its token for the bare service URL — so a path there would mean two audiences for one check and the tick would read as forged | `infra/terraform/envs/*/main.tf` |

### Four more rows for the table above

Three of these were found by **running the e2e suite repeatedly**, which is the habit
CLAUDE.md asks for and which paid for itself here: the first run was green, and the defects
appeared on the third, the fourth, and under full-suite load. The fourth was found by the
backend suite failing *because of the time of day*.

| Trap | How it presents | Where it will recur |
| --- | --- | --- |
| **A guard evaluated against the wall clock makes a suite pass or fail by the hour** | Every auto-scheduled assertion in `test_scheduler.py` failed on its first run, at 00:44 UTC, inside the *default* 23:00–07:00 quiet window. It read as a broken guard and was a working one | Anything reading a real clock: quiet hours, the cooldown, `postponedUntil`. Pin the input in the fixture — `start == end` here — rather than hoping the suite runs in daylight |
| **"Running with an expired lease" stays true of a crashed run forever** | `list_stuck` re-enqueued a run once per tick for the life of the ledger row, each time paying for whatever step its cursor was on. Invisible until the third consecutive e2e run, as a wall of `Aborted: Transaction lock timeout` on writes with no connection to any run | Any recovery query whose predicate is a *state* rather than an attempt count. The poison-pill bound belongs beside the query, and the run has to be *buried* rather than merely skipped or it is rediscovered forever |
| **A local stand-in without the production rate limit is not the production path** | Cloud Tasks caps `max_concurrent_dispatches` at 5; `InProcessQueue` started every run the tick scheduled at once. Ten concurrent research turns against one emulator surfaced as lock timeouts on an ordinary `POST /tasks` the browser had just made | Every double behind a `Protocol`. "The same code path" is only true of the code — a limit the real thing enforces has to be enforced by the stand-in, or the local run is a different system that happens to compile |
| **A fairness key that is never written starves the queue behind it** | Candidates are ordered `lastAutonomousRunAt ASC`, nulls first — and a project that is always *skipped* never gets one, so it holds the head of the order permanently. A hundred quiet-hours projects filled the window and the test's own new project was never examined; the tick reported a hundred skips and nothing else | Any cursor ordered by a field only the *success* path writes. Scanning past the skips fixes it (`TICK_CANDIDATE_SCAN`); the general shape is that a window sized to the cap assumes every candidate is viable |

**A fifth thing, which is a test-design trap rather than a defect.** Flow #8 asserted
`scheduled.length === 1` — a property of the *whole database*, which the e2e shares with
every other spec running concurrently. An assertion about a global endpoint has to be
filtered to the caller's own rows, or it is an assertion about what the rest of the suite
happens to be doing. The same test also raced the presence heartbeat: waiting for the
workspace heading is not waiting for the socket to have told the server anything, and
`framesent` is not the frame having been *handled*. It now waits for the effect — a tick
that reports the skip — with the project's work parked so the barrier cannot cause what it
is waiting to observe.

### Two more rows, found by the hand verification rather than the e2e suite

Both are RUNBOOK §10 defects: the first appeared only against real Cloud Tasks (its dedup
window is not a thing `InProcessQueue` has), and the second only appeared once the first
was fixed and a run's three attempts could actually play out instead of dying orphaned on
the first.

| Trap | How it presents | Where it will recur |
| --- | --- | --- |
| **A ledger patch committed before the write it was made *for* is an orphan if that write fails** | `_recover` patched a run to `pending` and incremented `attempts` *before* calling `enqueue_run`. Cloud Tasks' dedup threw `ALREADY_EXISTS` — the task name was keyed on `run_id` alone, so a retry collided with its own previous attempt's name for up to an hour — and the patch had already committed. Neither recovery query matches `pending` (`list_stuck` wants `running`, `list_retryable` wants `failed`), so the row was an orphan no future tick would touch, on a queue double that never fails locally | Any two-step sequence where the first step's state only makes sense once the second succeeds. Either order the write to depend on the call succeeding, or revert it if the call fails — `_recover` and `_schedule` (`services/scheduler.py`) both do the latter now |
| **A guard meant for unprompted work can silently swallow a prompted one** | `_research` skipped whenever `needsResearch` was false, with no exception for `trigger: "requested"` — even though `select_next_task` resolves a requested task's research unconditionally (this doc's own line: "a run that took the project because something was requested has to research that thing"). A learner pressing "prepare this" on a `propose_tasks`-authored subtask got a run that could never succeed, identically on all three attempts, and nothing local ever selects a task this way (`wants_auto_research` requires `needsResearch: true`, so only a *requested* run can reach it) | Any per-step guard added after the four owner-facing guards (presence, cooldown, `autonomousEnabled`, quiet hours) that does not ask whether it, too, is a default about *unprompted* work that a prompted run should bypass |

---

## M6 — Splitting the coach into a project coach and a task teacher (~0.5 week)

- **Split the one coach agent into two.** Reported from use after M4: asked to add optional
  topics to a study plan, the coach reaches for `add_task` — putting them on the *board* —
  where the learner meant `add_subtask`, inside the work in front of them. The board is one
  level up from the task the conversation is about, and one instruction serving both a
  project-level conversation and a task-level one has to keep saying which is which.

  The shape to build: a **project coach** for the intake session, which reasons about the
  board and has no item tools at all; and a **task teacher** for a task's session, which
  owns the checklist, the guided/unguided distinction, and `add_subtask`, and knows that
  everything it does is inside one entry of a list it can see. `MODE_KEY` already carries
  the distinction and the instruction already branches on it in prose, which is the version
  of this that does not work well enough.

  It lands on its own branch, ahead of the rest of the learner-model work in M7, because it
  is a rework of the agent graph rather than a wording fix — and because M7's profile-driven
  prompt changes are cheaper to make once, against two purpose-built instructions, than once
  against a single instruction serving two audiences and then again after the split. A
  prompt clarification went in after M4 as the cheap half.

**Exit:** Playwright flows #1, #2, and #7 still pass with two agents instead of one; asked
to add optional topics to a study plan, the project coach proposes `add_task` from the
intake session and the task teacher proposes `add_subtask` from the task session — never
the reverse; the project coach has no item-level tool available to it at all.

---

## Status after M6

**Met.** Golden flows #1, #2, and #7 pass on all four Playwright projects with
`project_coach` and `task_teacher` in place of the single coach, alongside every earlier
spec: 39 specs, 156 runs, including a full run of `coach.spec.ts`'s "discarding a task
waits for the learner to say so" from inside a task's own workspace — the spec that
exercises `discard_task` through `task_teacher` rather than `project_coach`.
`project_coach`'s catalogue (`DomainTools.as_project_tools`) has no item-level tool at
all; `task_teacher`'s (`as_task_tools`) has no `add_task` — the structural half of the
fix, checked by name in `tests/test_agent_tools.py` rather than left to the instruction.
586 backend tests, 281 web.

**Verified by hand on `coach-dev`**, against a real model rather than the stub: both
agents are live and, in ordinary use, doing roughly what they were designed to — the
project coach proposing and sizing board-level work from intake, the task teacher working
a task's own checklist without reaching for `add_task`. No deploy-only surface is new here
(no second Google integration, no Terraform change), so this milestone had no hand
verification in its exit criteria the way M3's and M5's did; it was done anyway; nothing
further was found.

**The split is a routing decision, not an instruction branch.** `TurnService._resolve_agent`
reads the turn's own session linkage — `task_id is None` or not — and picks
`RunnerFactory.project_runner()` or `.task_runner()` before generation starts; the public
`AgentChoice` literal callers pass (`"coach"`, from the turns router, unchanged) is resolved
to the concrete one internally, so nothing outside `services/turns.py` had to change to
route correctly. `MODE_KEY` — the state key the old single instruction branched on in prose
— is gone from `agents/prompt.py` entirely: once the branch became two instructions, nothing
read it.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| `update_task`, `set_task_state`, `set_next_up`, `reorder_task` on `task_teacher` | — | Kept `project_coach`-only. Nothing in the current design or tests asks for "postpone this task" or "re-estimate this task" from inside its own conversation; add them to `as_task_tools` if that turns out to be wanted, rather than pre-emptively |
| Everything still open after M5 | as recorded | Content scanning (M9), nightly evalsets and live-API tests (M7), `prod` and `terraform destroy` |

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| `discard_task` and `ask_learner` are on **both** catalogues | [09-roadmap.md](#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher)'s own sketch only says `task_teacher` "owns the checklist … and `add_subtask`", but a pre-existing regression test drives "discard that task" from inside a task's own session (`tests/test_discard_confirmation.py`, `coach.spec.ts`), and `ask_learner` was already reachable from the board session. Removing either to make the split cleaner would have failed a test that predates this milestone | `agents/tools.py` |
| `add_subtask` is on **both** catalogues, not `task_teacher`-exclusive | Golden flow #2 breaks an oversized task into subtasks in the same intake turn that created it (`add_task` then three `add_subtask` calls, all from `project_coach`). Giving `task_teacher` the exclusive claim on `add_subtask` would have broken that flow; the tool that is exclusive to `task_teacher` is `add_task`'s absence, not `add_subtask`'s presence | `agents/tools.py` |
| `services/turns.py` resolves `"coach"` to `"coach_project"` / `"coach_task"` **once, in `start`**, using the same linkage read that already existed for ownership checking | The alternative — branching inside each agent's `before_agent_callback` — is exactly the "one instruction serving two audiences" shape M6 exists to remove. Resolving before generation starts is also what makes the choice testable without a running turn (`test_the_interactive_agents_still_have_discard_task`) | `services/turns.py` |

**One trap recurred, in a new place.** `integrations/stub_model.py`'s tool loop used
`"add_task" not in tools` to mean "an agent with no domain tools at all" — true throughout
M3–M5, when the only such agent existed for the disconnect suite's plain-streaming double.
Splitting the coach made it false: `task_teacher`'s real, registered tool set also has no
`add_task`, so unless the check changed, `discard_task`, `ask_learner`, and
`complete_task_item` would have gone silently unreachable through the stub on that agent —
every test that drives them from a task session would have hung or fallen back to prose,
and nothing local would have named the reason. Fixed by checking membership in the full
known tool vocabulary (`_DOMAIN_TOOLS`) instead of the presence of one specific tool. The
general shape, for the table in [09-roadmap.md](#what-a-green-local-run-does-not-prove):
**a test double's gating logic can encode an assumption about the *set* of production tool
catalogues, which a later milestone that adds a catalogue silently breaks.** Any future
split of an agent's tools is the place to check a hand-written double's tool-presence
checks again, not just its scripted call sequences.

---

## M7 — Learner model and adaptation (~1 week)

- **Task-level preference overrides**, which [02-data-model.md](02-data-model.md#task-items)
  and `agents/tools.py` both anticipate: the checklist-size guidance currently reads the
  *project's* default task length, and a single task that is deliberately longer has no way
  to say so.
- `CoachMemoryService` + contract suite; `load_memory` wired into the task teacher (and the
  project coach, if intake conversations turn out to want it too).
- Session-close summarization into memory; `update_learner_profile` typed tool with
  versioning and an audit trail.
- Prompt construction consumes the profile; guidance style and verbosity visibly change
  behaviour.
- "What your coach knows about you" settings screen with per-field edit and reset.

**Exit:** across three sessions the coach demonstrably adapts (evalset check); every
profile change is attributable to a session and reversible by the user.

---

## Status after M7

**Met.** The learner model and adaptation suite is fully implemented and tested. Across multiple
sessions the coach demonstrably adapts (`tests/test_adaptation.py`), recording profile changes
audited with session attribution and rate-limited to 1 call per turn. Every profile change is
reversible and editable by the user on the "What your coach knows about you" Settings screen.
`CoachMemoryService` subclasses ADK's `FirestoreMemoryService` with `users/{uid}/memories/{memoryId}`
placement and contract tests against `InMemoryMemoryService`. Task-level duration overrides are
respected in checklist guidance. 606 backend tests, 285 web tests.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| Split research runs into dedicated sessions | **M8** | Moves research events out of the task conversation into job-scoped sessions |
| UI rework for workspace and transcript | **M8** | Scope decided once research sessions are decoupled |
| Per-user token and run quotas backed by `usage/*` | **M8** | Quota enforcement on the reworked run/session boundary |
| Nightly live-API and evalset runs | **M9** | Live API tests against real Gemini/YouTube |
| Content scanning on finalize | **M9** | Still as recorded after M2 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1 |

---

## M8 — Research sessions, UI rework, and usage quotas (~1.5 weeks)

Scope settled at the start of the milestone, before any code changed — the three
sub-sections below are the decisions, not a proposal.

- **Split research runs out of the task session into a session of their own, per research
  job.** A research run currently executes as an ordinary turn inside the task's own
  session — decided at M4 specifically so it could reuse the disconnect guarantee,
  checkpoints, and broker without a second implementation ([status after M4](#status-after-m4):
  "a research run is an ordinary turn … one argument to `TurnService.start` selects the
  agent"). This milestone keeps that reuse but gives each run its own session, created
  fresh and never reused, so a task's conversation transcript stops interleaving with
  tool-heavy research turns the learner never took part in
  ([02-data-model.md](02-data-model.md#sessions--events-adk-owned-layout)).
- **Research becomes possible with no parent task.** Kicked off from the project coach's
  own conversation rather than a task's, it researches a free-standing question about the
  project as a whole; the run and its report carry `taskId: null`, and nothing is promoted
  into any task's checklist. The dedicated session still carries a `taskId` — the *parent*
  task for a task-scoped run, `null` for this case — the same field either way
  ([03-agent-design.md](03-agent-design.md#research_agent)).
- **UI rework.** A dedicated research view (`/projects/:projectId/research/:runId`), two
  panes: the research session's own transcript (read-only — no composer, the coach is not
  conversing with anyone) and the final report with citations, once there is one. The
  board and the task workspace each show a compact "latest research" card linking into it,
  replacing the report block the task workspace used to render inline
  ([06-frontend.md](06-frontend.md#research-view-projectsprojectidresearchrunid)).
- **Rate limits, per-user daily token and run quotas, `usage/*` counters — deferred to a
  follow-up slice, not built in this pass.** The roadmap's original reasoning for bundling
  them here still holds (quota enforcement hangs off the same run/session boundary this
  milestone reworks, so building it against the new shape once is cheaper than building it
  against the old shape and migrating at M9) — but the session split and the UI rework were
  the two items with an actual specification going in, and quotas were not scoped at all.
  Splitting them out rather than guessing at a design keeps this milestone's exit criteria
  honest. Still belongs before M9, on the same reasoning as above.

**Exit:** a research run's events live in a session scoped to that run, not the task's;
the task session's transcript reads as conversation between the learner and the task
teacher; research can be requested with no task attached and produces a `taskId: null`
report; the research view renders a run's transcript and its final report side by side;
the board and task workspace each surface the latest research job as a card. Quotas are
tracked as a separate follow-up with their own exit criteria, to be written when that slice
starts.

---

## Status after M8

**Met**, for the two sub-sections that had a specification going in. Research runs —
manual and scheduled alike — now create a dedicated session per run
(`CoachSessionService.create_research_session`, `kind: "research"`) and never write into
a task's own conversation; `propose_tasks` was moved onto the same run session for the
same reason, since it is board maintenance the learner did not ask for in the moment, not
a reply to them. Research is now reachable with no parent task, from the project's own
intake conversation — `POST /api/sessions/{sid}/research` accepts a session with `taskId:
null` and requires a non-empty `reason` in that case — and produces a `taskId: null`
report that validates the same way a task-scoped one does but promotes nothing anywhere.
The dedicated research view (`/projects/:projectId/research/:runId`) renders the run's own
transcript (read-only `SessionPane`) beside its report once there is one; the board and
the task workspace each show a compact "latest research" card (`ResearchCard`, fed by
`GET /api/projects/{id}/runs` and the new `GET /api/tasks/{id}/runs`) linking into it,
with a "View previous research (N)" toggle beside it listing the rest of that same feed —
each earlier run's report fetched lazily, only once the toggle is opened. The inline report
block the task workspace used to render is gone; reports still accumulate in Firestore
exactly as before (Q4, [10-risks.md](10-risks.md#open-questions)), and the toggle is what
keeps that history browsable from the UI rather than only reachable by URL. 619 backend
tests, 292 web tests, the e2e suite (chromium) green including two new specs for the
taskless path.

Two things worth a future reader's attention, neither large enough to hold up the exit:

- **`budgetMinutesOverride` on the manual research request is still accepted and recorded
  on the run, and still not threaded into the model's own budget** — a gap that predates
  this milestone (M4) and that M8 did not have cause to close. A task-scoped run's budget
  is the task's `estimatedMinutes`; a taskless run's is the project's `defaultTaskMinutes`.
  Wiring the override properly means also changing what `render_budget`'s prose tells the
  model, not only what `post_research_report` validates against — the two have to agree,
  or the model is validated against a number it was never shown. Left alone rather than
  half-fixed.
- **A taskless report's `optional[]` items render with no feedback control.**
  `PATCH /api/reports/{reportId}/items/{itemId}` checks ownership through the task
  (`ReportItemFeedback.taskId`), which a `taskId: null` report has none of. Giving feedback
  a second ownership path for a thumbs-up on further reading was not worth doing until
  project-scoped research is more than a first cut.

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| Per-user daily token and run quotas, `usage/*` counters, rate limits | **M8-quotas**, before M9 | Scoped out at the start of this slice because it had no specification going in, unlike the session split and the UI rework. The reasoning for building it against the new run/session shape rather than the old one still holds |
| Nightly live-API and evalset runs | **M9** | Live API tests against real Gemini/YouTube |
| Content scanning on finalize | **M9** | Still as recorded after M2 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1 |

---

## M8-quotas — Per-user usage quotas, coupons, and abuse-prevention rate limits (~1 week)

Scoped out of M8 itself for having no specification going in
([status after M8](#status-after-m8)); the decisions below are that specification, settled
at the start of this slice rather than mid-flight.

- **Two usage windows per user — monthly, and a 4-hour burst limit — denominated in
  points, where 1 point = 1,000 tokens.** All LLM API usage counts against both; a new
  call is refused once either window is exhausted, and each resets independently. The
  free preset is 500 monthly / 80 four-hour
  ([02-data-model.md](02-data-model.md#usage-quotas-m8-quotas)).
- **Enforcement is one gate on `TurnService.start`, covering everything**: interactive
  coach/task-teacher turns, manual and requested research, and scheduled autonomous work
  all converge there already (docs/09-roadmap.md#status-after-m4), so the pre-flight check
  and the post-turn spend recording are written once. The existing
  `plan.limits.autonomousRunsPerDay` pacing cap on background work is untouched and
  unrelated — points are a cost ceiling layered on top of it, not a replacement.
- **A new account's limits are materialized from `plans/{tier}` at creation**, not computed
  from a formula at read time, so an existing user's limits cannot move under them when the
  free tier's default changes later.
- **Coupons.** `coupons/{code}` — single-use, hand-written during beta — grant a *replacement*
  set of monthly/4-hour limits to the claiming account, letting a trusted beta tester
  use the service as though subscribed, ahead of real billing. `POST /api/coupons/claim`.
- **Two abuse-prevention rate limits**, both Firestore sliding-window counters on the same
  shape `board_events/{uid}` already established for cross-instance state: new account
  creation (4 / 30 min, global) and coupon-claim attempts (5 / hour / uid, recording wrong
  guesses too) — the first bounds free-tier signup abuse directly, the second bounds
  brute-forcing a coupon code, on top of what the first already costs an attacker.
- **A retry affordance for a quota-blocked send.** Since a blocked turn is never created,
  there is nothing to resume the way a disconnected turn is resumed — the chat pane instead
  keeps the message that was refused and offers a plain "Retry" that resends it unchanged,
  once the reader believes the window has reset.

**Exit:** a fresh account's `plan.limits` matches `plans/free`; a turn is refused with
`429 quota-exceeded` the moment either window is spent, for an interactive turn exactly as
for a research or autonomous run; an exhausted auto-scheduled project is simply a candidate
again on the tick after its window resets, with no special recovery path; claiming a
coupon replaces the claiming account's points limits and a second claim of the same code
is refused; more than four accounts created inside 30 minutes, or more than five
coupon-claim attempts inside an hour for one account, are rate-limited; a chat turn blocked
by quota shows a retry control that resends the same message.

---

## Status after M8-quotas

**Met.** `plans/free` (500 monthly / 80 four-hour / 20 `autonomousRunsPerDay`) is what
`UserService.get_or_create` copies onto a new account, falling back to `PlanLimits`'s
Python defaults — numerically identical — when the preset document is absent, so a fresh,
unseeded emulator behaves the same as a seeded one. `TurnService.start` calls
`QuotaService.require_available` before creating a turn and `QuotaService.record_spend`
once generation ends on every terminal path (`complete`, `cancelled`, `failed`), summing
`usage_metadata.total_token_count` across the turn's non-partial model-response events;
`SchedulerService._shared_guards` checks the same two windows before scheduling either
kind of autonomous work, recording `points_quota_exhausted` distinctly from the
pre-existing `quota_exhausted` (run-count) skip. `POST /api/coupons/claim` replaces
`plan.limits.{monthlyPoints,fourHourPoints}` transactionally and is refused a second time
on the same code; both new rate limits are Firestore sliding-window counters
(`rate_limits/new_users`, `rate_limits/coupon_claim:{uid}`) that record only what should
count against the window (a rejected attempt records nothing at registration; a wrong
coupon guess still records, since brute-forcing wrong codes is exactly what that limit
exists to slow down). The chat pane keeps a quota-blocked send's text and attachments and
offers a "Retry" button that resends them unchanged, distinct from the pre-existing "You
can try again" text for a turn that failed *after* starting. 650 backend tests, 303 web
tests, 41 Playwright specs (chromium) — all green, including the full e2e suite run
against the built image with the real quota gate active throughout.

**The e2e harness needed one accommodation, decided at the start of this slice rather than
found by surprise**: `e2e/fixtures.ts` mints a fresh, never-reused uid per *test* as its
whole isolation strategy, against one long-lived compose stack — the production default of
4 new accounts per 30 minutes would have failed the fifth spec of every run on a guard that
has nothing to do with the behaviour under test. `NEW_USER_RATE_LIMIT` /
`NEW_USER_RATE_LIMIT_WINDOW_MINUTES` are `Settings` fields rather than module constants for
exactly this reason; `docker-compose.e2e.yml` raises the limit, every other environment
keeps the default of 4/30 min
([04-api-contract.md](04-api-contract.md#abuse-prevention-limits-implemented-m8-quotas)).

**Deliberately deferred, and the milestone that needs it:**

| Item | Needed by | Note |
| --- | --- | --- |
| Nightly live-API and evalset runs | **M9** | Live API tests against real Gemini/YouTube |
| Content scanning on finalize | **M9** | Still as recorded after M2 |
| `prod` environment, `terraform destroy` | before release | Still as recorded after M1 |
| Billing (Stripe, plan tiers metered off `usage/*`) | post-v1 | The counters this milestone writes are the ones a metered plan would read; nothing here bills anyone |

**Decisions made during implementation** that the design documents did not fix, settled
before any code changed (this milestone had no specification going in — see
[status after M8](#status-after-m8)):

| Decision | Why | Where |
| --- | --- | --- |
| 1 point = 1,000 tokens, `ceil`'d | A round, easy-to-explain unit; rounding up rather than down means a turn that used any tokens at all always costs at least one point, so nothing is free by truncation | `repositories/usage.py` |
| The 4-hour window is six **fixed** timezone-local blocks a day, not a rolling window | One point read and one `Increment`, the same bucketing tradeoff `usage/{uid}_{yyyymmdd}` already made for the daily run-count quota at M5, rather than a query over a trailing range | `repositories/usage.py` |
| Two windows (monthly, 4-hour), not three | A third, daily window was redundant next to the 4-hour burst limit and made the feature harder for a learner to reason about — two numbers is what fits in a settings screen's meters without a footnote | `services/models.PlanLimits`, `repositories/usage.py` |
| A coupon's grant is `CouponLimits`, a distinct model from `PlanLimits` | A coupon is about spend; giving it `autonomousRunsPerDay` would let a coupon document imply it also changes pacing, which nothing reads it for | `services/models.py` |
| `plan.limits.autonomousRunsPerDay` is untouched by everything this milestone added | Kept as the existing pacing cap on background work, independent of the new cost ceiling layered beside it — the two are different guards for different reasons, not one superseding the other | `services/scheduler.py`, `services/coupons.py` |
| The pre-flight check and the spend recording are both in `TurnService`, nowhere else | Every interactive turn, research run, and autonomous pass already converges on `TurnService.start` (docs/09-roadmap.md#status-after-m4); a second gate anywhere else would be a second place to keep in sync | `services/turns.py` |
| Token spend is recorded in `_generate`'s `finally`, unconditionally | Tokens already spent are already spent regardless of how the turn ended — the same "counted when spent, not when the outcome is good" reasoning `record_autonomous_run` already applies | `services/turns.py` |
| A blocked turn gets a **new** `Retry` control, not the existing `turn_error` retry text | A quota-exceeded refusal never creates a turn, so there is no `turn_error` frame to attach "you can try again" to — the chat pane has to keep the message that was refused somewhere of its own | `components/session/SessionPane.tsx` |
| The new-account rate limit is a `Settings` value, not a constant | `e2e/fixtures.ts` mints a fresh uid per test as its whole isolation strategy; a hardcoded production default would fail the fifth e2e spec of every run. See the paragraph above | `core/config.py`, `docker-compose.e2e.yml` |
| A rejected rate-limit attempt is never recorded | Recording it would advance the window's own oldest timestamp, letting a caller who keeps retrying shorten its own wait — the opposite of what the limit is for | `repositories/rate_limits.py` |
| A wrong coupon guess **is** recorded against the claim-attempt limit | Brute-forcing codes is exactly what that limit exists to slow down; only the *account creation* limit gets the "don't record a refusal" treatment, and for the opposite reason — there the refused action is legitimate traffic, not an attack | `services/coupons.py` |

### Two more UX changes on the same branch, neither its own milestone

Requested after the milestone above was already green, and small enough to land beside it
rather than opening a second branch.

**The usage meters show a countdown, not only a timestamp.** `UsagePlanCard`'s 4-hour row
reads "Resets 8/24/2026, 8:00:00 PM (in **0h 34m** from now)" — the monthly row does not,
since a countdown in hours and minutes is the wrong grain for a window that resets in days.
Computed at render time from `Date.now()` rather than a ticking clock: the value is
already refreshed on every `GET /api/me` poll, and a settings screen is not somewhere a
stale minute matters enough to justify a `setInterval`.

**A learner may set their own display name (`PATCH /api/me`), and it replaces email as the
header's primary, visible identity — email moves to the hover title.** This touches M0's
own exit criterion above, which the header comment used to state literally ("the signed-in
user sees their email"): the *proof* that criterion cared about — the token round-tripped
through `verify_id_token` and the user document resolved — is unchanged and still visible
(now on `title` rather than as the element's text), but the visible text is not. Rather than
silently reinterpret a `docs/09-roadmap.md`-referenced comment, this was confirmed with the
user before changing: keep the round-trip proof, move email to the tooltip, prefer the
display name. `e2e/board.spec.ts`'s M0 assertion moved with it —
`getByTestId('signed-in-identity')` is non-empty and its `title` contains `@`, in place of
the old `signed-in-email` element's own text doing both jobs at once.

**`display_name_customized` is why this doesn't get silently reverted.**
`UserService.get_or_create` refreshes `displayName` (among other fields) from the sign-in
token's own claim on every request — the mechanism that follows a Google account's name or
avatar automatically. Once a learner sets their own, that one field is excluded from the
refresh for their account, permanently; nothing else about the loop changes. Without this,
the very next `GET /api/me` — which every screen makes — would silently overwrite the
learner's choice with whatever the token still says.

---

## M9 — Hardening and launch readiness (~1.5 weeks)

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
convenient. `M7` depends on M6, since its profile-driven prompt changes assume the
project-coach/task-teacher split already exists. `M8` depends on M4 and M5 for the research
and run machinery it reworks. `M8-quotas` depends on M8 for the run/session boundary it
gates and on M5 for the `usage/*` collection and `autonomousRunsPerDay` guard it extends.
`M9` closes out the list. Roughly 14.5 weeks of sequential work, ~12.5 with the M3/M4
overlap.
