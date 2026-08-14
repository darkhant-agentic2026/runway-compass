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
