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

**M2 is met.** The last exit criterion was verified by hand on `coach-dev` on 2026-08-17:
a signed-in user chats with the coach, attaches a screenshot by paperclip and by
drag-and-drop, gets a reply that demonstrably sees the image, reloads, and sends a further
turn in the same session. Vertex AI, V4 signed upload URLs, and `GcsArtifactService` are
therefore all exercised for real, which no local test can do.

Eight defects were found and fixed closing it, six of them reachable only in a deployed
environment; all are itemised under *Fixed after the first M2 pass* below. That is the
number worth remembering when planning M4 and M5 — a green local gate said nothing about
any of the six.


**Met, locally.** The full disconnect matrix is green, including cross-instance resume
(driven by two `Container`s over one emulator, which is what a second Cloud Run instance
is). Golden flow #4 passes on all four Playwright projects — chromium, mobile-chrome,
webkit, and mobile-safari — which is what installing WebKit early was for. 284 backend
tests, 142 web, 52 Playwright specs.

**How the last criterion was closed:** by deploying to `coach-dev` and doing it by hand,
following [RUNBOOK.md section 8](../infra/terraform/RUNBOOK.md#8-closing-the-m2-exit-criterion).
Four things on that path have no local equivalent — Vertex AI as the model backend (local
and e2e both run a scripted or stub model), V4 signed upload URLs (which need a real IAM
signer), `GcsArtifactService`, and whether Vertex resolves the `gs://` artifact URI a turn
attaches — so the deploy was not a formality, and each of the four had a real defect
behind it.

**Decisions made during implementation** that the design documents did not fix:

| Decision | Why | Where |
| --- | --- | --- |
| The cross-instance resume path **polls** `turns/{turnId}` every 400 ms instead of using a snapshot listener | [04-api-contract.md](04-api-contract.md#surviving-client-disconnects) says "follows the Firestore document with a snapshot listener", but `on_snapshot` is implemented only on the **synchronous** `DocumentReference`; on `AsyncDocumentReference` it inherits `BaseDocumentReference.on_snapshot`, which raises `NotImplementedError`. The async client is not optional — ADK's shipped session service is async throughout. Polling at the checkpoint interval delivers exactly the granularity that section already accepts for this path ("400 ms chunks instead of token-level, still correct"), so the promise is kept and the mechanism differs | `ws/manager.py` |
| `checkpoints[].lengths` added to the turn document | Makes resume exact when `lastSeq` falls inside a slice, rather than duplicating or dropping text ([02-data-model.md](02-data-model.md#turnsturnid)) | `services/models.py` |
| `ws_tickets/*` and `uploads/*` added to the collection map | Cross-instance state the contract needs and no existing collection holds ([02-data-model.md](02-data-model.md#collection-map)) | `repositories/` |
| **`MODEL_BACKEND=stub`**, refused for any `ENV` other than `local` | [08-testing.md](08-testing.md) asks for "a stubbed model server"; a stubbed *model* is the same determinism for a fraction of the surface, since nothing in the socket, checkpoint, or resume path can tell where tokens came from. Guarded and regression-tested like the `Bearer dev:<uid>` path, because its failure mode is *silent success* | `integrations/stub_model.py` |
| A cancelled turn is announced as `turn_error` with `code: "cancelled"`, `retryable: false` | The contract has no `turn_cancelled` frame, and the user asked for this — offering a retry would be wrong | `services/turns.py` |
| `GET /api/turns/{turnId}` added | Lets a client with a dead socket tell a running turn from a finished one, so the "still working" state is truthful rather than hopeful | `api/routers/sessions.py` |
| The `StreamBroker` keeps a per-turn ring buffer of recent frames | Deltas are published immediately but checkpointed up to 400 ms later; a client attaching in that window would find a frame in neither source. The buffer covers exactly that gap | `ws/broker.py` |
| An upload's artifact is `user:`-scoped and named for its upload id | ADK scopes an artifact to a session or to a user, and `POST /api/uploads` does not know a session — the contract's body is `{ filename, mimeType, sizeBytes }`. The id rather than the filename so two `screenshot.png`s are two artifacts, not two versions of one | `integrations/artifacts.py` |

**Known limitation: `gs://` attachments do not work against `MODEL_BACKEND=gemini_api`.**
[00-overview.md](00-overview.md#model-configuration) specifies attachments as "`types.Part`
file references backed by GCS", which Vertex AI resolves and the Gemini API does not — it
wants inline bytes or its own Files API. Production is Vertex, so the shipped path is
correct; a developer running locally against a real Gemini key will find that text works
and attachments do not. Not worked around, because the workaround is a second multimodal
code path that production would never exercise.

### Fixed after the first M2 pass

Two gaps found by auditing the M2 surface against the contract rather than against the
tests, which is why neither showed up as a failure:

- **`POST /api/uploads/{id}/finalize` did not register the ADK artifact.** It verified
  size and type and stopped, and the turn referenced `gs://{project}-coach-uploads/…`
  directly. That bucket is staging and carries `lifecycle_rule { age = 1 → Delete }`; a
  GCS lifecycle rule cannot express "unfinalized", so it collects finalized objects too.
  Because a session's history is replayed to the model on every subsequent turn, the
  symptom would have been delayed and silent — a coach that had forgotten a screenshot it
  discussed the day before. Finalize now copies the verified bytes into
  `{project}-coach-artifacts` through `GcsArtifactService` and every later reference uses
  the artifact URI. Nothing local caught this: the in-memory object store has no lifecycle
  rule, and no test waits a day.
- **"Scans" is still not implemented.** The contract lists content scanning in the same
  step. Deferred to M7's "Security review: … upload handling" rather than left unremarked;
  until then an accepted MIME type and a size cap are the only checks on uploaded bytes.
- **`POST /api/tasks/{id}/session` returned 500 on the first deployed revision.**
  `find_session_id_for_task` filtered on `taskId` *and* `appName`, which makes it a
  composite collection-group query; the declared index
  (`google_firestore_field.sessions_task_id`) is single-field, exactly as the index table
  in [02-data-model.md](02-data-model.md#indexes) specifies. Real Firestore answered
  `FAILED_PRECONDITION` and the emulator answered correctly, so the whole local gate —
  291 backend tests, the disconnect matrix, 52 e2e specs — was green against a query that
  could not run in production. The `appName` check moved into Python, and a test now pins
  the filter count, because no result-level test can see this.

  Two unused queries in `repositories/turns.py` had the same defect latent
  (`instanceId` + `status`, and `status` + `leaseExpiresAt`) and were deleted rather than
  indexed: nothing called them, and M5's ledger sweep should add each query together with
  its index and its row in the index table.

  The general rule is now the first thing in the footgun list in `CLAUDE.md`. **The
  emulator not enforcing index requirements is the single widest gap between the local
  gate and a deployed environment**, and it is worth assuming it will bite again at M4
  and M5, which add the research and run queries.
- **`POST /api/uploads` 500'd on Cloud Run, invisibly.** `GcsObjectStore` called
  `generate_signed_url()` on the self-signing path. Cloud Run's
  `compute_engine.Credentials` have no private key and no `signer_email`, so that raises;
  local development uses impersonated credentials, which *can* sign for themselves, so it
  passed there — precisely the failure
  [07-infra-deploy.md](07-infra-deploy.md#the-two-local-dependencies-that-are-not-emulated)
  predicted ("only in a deployed environment"). The IAM binding it calls for was already
  granted; the code simply never used it. Signing now passes `service_account_email` and
  `access_token` when the credentials cannot sign for themselves, which is what routes it
  through IAM `signBlob`.

  **The 500 produced no visible change in the UI at all** — no chip, no error — because
  `toast.error` was the only reporting channel and **no `<Toaster />` was ever mounted**.
  A server error looked exactly like a dead event handler, which cost a round of
  diagnosis in the wrong half of the stack. Both are fixed, and both now have tests: the
  signer's argument choice, which no integration test can reach, and the toaster's
  presence, which no other test would notice because asserting `toast.error` *was called*
  stays true when nothing is listening.

  Also fixed alongside: dropping a file did nothing, because the drop target was the
  composer strip rather than the chat pane, and `google-cloud-storage` was undeclared —
  imported directly but arriving as a transitive of `firebase-admin`, the same situation
  `google-cloud-firestore` is pinned for.
- **…and then IAM `signBlob` refused the token: `ACCESS_TOKEN_SCOPE_INSUFFICIENT`.** The
  fix above reused the *storage client's* credentials for the IAM call, and
  `storage.Client()` resolves ADC scoped to storage. `iamcredentials.googleapis.com`
  accepts nothing narrower than `cloud-platform`. The 403 names IAM and reads exactly
  like the missing `serviceAccountTokenCreator` binding — but that binding was present
  and correct; the token simply was not allowed to exercise it. Signing now resolves its
  own `cloud-platform`-scoped credentials rather than borrowing the client's.
- **A bug was the only thing in the service that did not answer in `problem+json`.**
  Starlette's default 500 is `text/plain`, so the client's parser fell back to the HTTP
  status text and every unhandled failure reached the user as "request failed" — while a
  complete traceback sat in the logs with nothing tying it to the request. That is what
  turned the two upload failures above into three round trips. There is now a global
  handler returning a problem document with a `traceId`, taken from Cloud Run's
  `X-Cloud-Trace-Context` when present so the id in the response is the one
  `gcloud logging read 'trace:"…"'` matches. `detail` names the exception outside
  production and is a fixed string in it, since an exception message can carry a bucket
  name, a query, or a row of user data. The rest of M7's error-handling pass —
  retryability, wording, empty states — is still M7's.
- **Two transcript defects, found by using the thing.** Neither is deployment-specific;
  both are the kind only visible in a real conversation.

  The sender's own message did not appear until the turn *completed*. ADK writes the user
  event during generation and the transcript is refetched on `turn_complete`, so for the
  whole reply the sender saw nothing but "Your coach is thinking…" — no record of what
  they had asked. Now echoed optimistically into the events query, which is the shape
  [06-frontend.md](06-frontend.md#tanstack-query-server-state) prescribes for interactions
  that must feel instant, and which also avoids a duplicate: the refetch replaces the
  whole array, so the synthetic event is swapped for the stored one in one render rather
  than torn down in a second step.

  Attachments were invisible in history. `toMessages` reduced them to a count and then
  dropped it, so a message carrying both a question and a screenshot rendered as the
  question alone. Attachments are now carried through and rendered as chips — by filename
  while the message is in flight, and by kind afterwards, because a stored event holds a
  `gs://` artifact URI and no human filename. Writing the test for this also surfaced that
  an event carrying *only* tool calls became an empty bubble; those are dropped now.
- **…and the chip then vanished from *reopened* conversations.** That fix worked in the
  session that sent the file and nowhere else, because those are two different sources:
  the optimistic echo, which this project writes, and the stored event, which ADK does.
  `Event` declares `alias_generator=to_camel`, so its aliases are camelCase — but
  `append_event` stores `model_dump(exclude_none=True, mode="json")`, and `model_dump`
  defaults to `by_alias=False`. Firestore holds `file_data` / `mime_type`; the reader
  looked for `fileData` and found nothing.

  Every unit test passed, because the fixtures had been invented from the same wrong
  assumption — so the fix is not only the reader. `scripts/gen_event_vectors.py` dumps real
  events from the Python side into `session-event-vectors.json` and `transcript.test.ts`
  replays them, the same generated-parity approach the fractional index already uses:
  **an observed fixture cannot repeat the mistake that produced it.**
  `TurnService._build_content` also sets `file_data.display_name`, so a reopened
  conversation names the file rather than only its kind.

  Added at the same time, and **not in any milestone**: image attachments render as
  thumbnails rather than labels
  ([06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid)) — docs
  asked for preview thumbnails on the *composer* and said nothing about the transcript.
  That needed one new endpoint,
  `GET /api/sessions/{sid}/events/{seq}/attachments/{index}`, addressed by position rather
  than by artifact name or URI so that reaching an event at all proves ownership and no
  caller-supplied storage path is ever validated.
- **`gemini-3.7-flash` is served to `coach-dev` on the `global` endpoint, not in
  `us-central1`.** Every turn failed with `NOT_FOUND` naming the model. The model choice
  in [00-overview.md](00-overview.md#model-configuration) is unchanged and correct — the
  design simply never said where to call it, and `VERTEX_LOCATION` was hardwired to the
  Cloud Run region. It is now a `vertex_location` variable, set to `global` in
  `envs/dev/dev.tfvars` and deliberately left unset in `prod`, where `global` would be a
  data-residency decision rather than a configuration one. Also fixed alongside: the 404
  was reported to the user as retryable, so the UI invited a retry of a model that does
  not exist.

**The upload path now has end-to-end coverage**, via a local-only PUT receiver behind the
same `ENV=local` guard as `MODEL_BACKEND=stub` (`api/routers/local_storage.py`,
[08-testing.md](08-testing.md#end-to-end-playwright)). `e2e/attachments.spec.ts` attaches
by picker and by drop, sends, **reopens the workspace**, and asserts the attachment and its
preview are still there. Signing remains uncovered and unreachable locally, which is
inherent: it needs a real signer.

**Deferred, and the milestone that needs it:** the `subscribe`-by-`runId` frame is
accepted and answered with an explicit error until the run ledger lands (M5); tool-activity
chips are rendered from the live stream but are not replayed on resume, since they are not
checkpointed — a resumed client rebuilds them from the finalized transcript.

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
