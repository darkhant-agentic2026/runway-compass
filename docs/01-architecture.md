# Architecture

## Runtime topology

```
                    ┌──────────────────────────────────────┐
   Browser          │            Google Cloud              │
 ┌──────────┐       │                                      │
 │ Vite SPA │──HTTPS┼─▶ Cloud Run: coach-api (FastAPI)     │
 │  React   │──WSS──┼─▶   ├─ SPA   /  (static, in-image)   │
 └──────────┘       │     ├─ REST  /api/*                  │
      │             │     ├─ WS    /ws                     │
      │ Identity    │     ├─ INT   /internal/*  (OIDC)     │
      │ Platform    │     └─ ADK Runner + agent graph      │
      │ (ID token)  │            │        │        │       │
      │             │            ▼        ▼        ▼       │
      └─────────────┼──▶ Identity  Firestore    GCS        │
                    │    Platform  (domain +   (uploads,   │
                    │              sessions +   artifacts) │
                    │               memory)                │
                    │                                      │
                    │  Cloud Scheduler ──▶ /internal/tick  │
                    │        │                             │
                    │        └──▶ Cloud Tasks ──▶ /internal/runs/{id}/execute
                    │                                      │
                    │  Vertex AI (gemini-3.7-flash)        │
                    │  YouTube Data API v3 (external)      │
                    └──────────────────────────────────────┘

Frontend hosting: the built SPA ships inside the API image and is served by Cloud Run
itself, so the browser sees one origin with no CORS and no third-party-cookie surface, and
no load balancer is required. Cloud CDN in front of the service is the later upgrade if
static-asset latency ever shows up in monitoring.
```

## Components

### `coach-api` (Cloud Run, Python 3.12, FastAPI)

One service, four surfaces:

- **`/`** — the built SPA, mounted from `static/` in the image. The catch-all fallback to
  `index.html` is registered *after* every router below, so it cannot shadow them.
- **`/api/*`** — authenticated REST for the SPA. Identity Platform ID token in
  `Authorization: Bearer`.
- **`/ws`** — authenticated WebSocket for streaming turns and board-update pushes.
  Authenticated by a single-use ticket (browsers cannot set WS headers).
- **`/internal/*`** — invoked only by Cloud Scheduler and Cloud Tasks with Google-signed
  OIDC tokens; the Cloud Run service is *not* public for these paths (verified by
  audience + service-account email, and Cloud Run IAM `run.invoker`).

Single service rather than separate web/worker services because the WebSocket fast path
for manually triggered research needs the run to execute in the same process the user is
connected to. The tradeoff (background work competing with request serving for CPU) is
managed with Cloud Run concurrency limits and a per-instance semaphore on agent runs.

### Layering inside `coach-api`

```
api/            FastAPI routers, request/response schemas, auth dependency
ws/             connection manager, ticket store, stream broker, resume replay
agents/         ADK agent definitions, tools, callbacks, prompts
services/       use-case layer: TaskService, ProjectService, RunService, ResearchService
repositories/   Firestore access; the ONLY module that knows collection paths
adk_firestore/  CoachSessionService / CoachMemoryService — subclasses of ADK's shipped
                Firestore services (docs/03-agent-design.md)
integrations/   Vertex/Gemini client, YouTube Data API, URL fetcher, GCS
core/           config, logging, tracing, errors, clock, id generation
```

Rule: **agent tools call `services/`, never `repositories/` directly.** The service layer
holds authorization checks, rollup maintenance, and event publication, so the agent and
the REST API cannot diverge in behaviour. A tool that wants to complete a task calls the
same `TaskService.complete_task()` the user's button calls.

### Repo layout (monorepo, no workspace tooling required)

```
self-study-coach-v1/
├── apps/
│   ├── api/                 # Python service (uv, ruff, pytest)
│   │   ├── pyproject.toml
│   │   └── src/coach/…      # layering above
│   └── web/                 # Vite + React + TS
│       ├── package.json
│       └── src/…
├── Dockerfile               # builds web + api into one image; context = repo root
├── infra/terraform/         # envs/{dev,prod} + modules
├── scripts/                 # dev.sh, emulators.sh, seed.py
├── .github/workflows/       # ci.yml, deploy-cloudrun.yml
└── docs/
```

## Three request paths worth tracing

### 1. Interactive turn (user sends a message)

1. SPA `POST /api/sessions/{sid}/turns` with text + attachment refs. Server validates
   ownership, creates `turns/{turnId}` (`status: running`, `seq: 0`), spawns a **detached
   asyncio task**, returns `{turnId}` immediately (~50 ms).
2. The detached task runs the ADK `Runner`, which resolves the session through
   `FirestoreSessionService`. Each streamed delta increments `seq`, is published to the
   in-process `StreamBroker`, and is appended to a rolling buffer flushed to
   `turns/{turnId}.checkpoints` every ~400 ms or 512 chars.
3. The SPA's existing `/ws` connection subscribes to `turnId`; chunks arrive with `seq`.
4. On completion the task writes the final ADK `Event`s via `append_event`, sets
   `turns/{turnId}.status = "complete"`, and publishes `turn_complete`.
5. If the socket dropped at any point, none of the above changes. The client reconnects,
   sends `{"type":"resume","turnId":…,"lastSeq":N}`, and the server replays checkpoints
   `> N` from Firestore, then attaches to the live broker.

### 2. Autonomous research

`Cloud Scheduler (*/15m)` → `POST /internal/tick` → for each candidate project: skip if
owner is present or a lease is held → create/resume `autonomous_runs/{runId}` → enqueue
Cloud Task → `POST /internal/runs/{runId}/execute` runs the research workflow step by
step, checkpointing each step. Full detail in [05-autonomous-runs.md](05-autonomous-runs.md).

### 3. Manual research on a session

`POST /api/sessions/{sid}/research` → same `RunService.start(trigger="manual")` → executed
inline in the request-handling instance (detached task, same streaming machinery as path 1)
so the user watches it happen live.

## Cross-cutting concerns

- **Authorization** is a FastAPI dependency producing a `Principal(uid)`; every service
  method takes it and asserts `project.owner_uid == principal.uid`. Repositories never
  filter by owner implicitly — an explicit check is easier to audit than an implicit one.
- **Idempotency**: all mutating REST endpoints accept `Idempotency-Key`; agent tool calls
  carry the ADK invocation id, stored on the resulting domain doc to make replays safe.
- **Observability**: structured JSON logs with `uid`, `project_id`, `run_id`, `turn_id`,
  `invocation_id`. OpenTelemetry traces from FastAPI → ADK → Vertex. A `token_usage`
  Firestore doc per user per day for cost tracking (and future quotas).
- **Cost control**: a per-user daily cap on autonomous runs, plus monthly and 4-hour
  points quotas on total model tokens that block interactive use too, not only autonomous
  work — see [02-data-model.md](02-data-model.md#usage-quotas-m8-quotas).
