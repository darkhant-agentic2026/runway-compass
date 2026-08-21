# Self-Study Coach — Overview & Key Decisions

An adaptive, agentic web app that helps a learner make progress on technical goals in
bite-sized tasks. The agent keeps a durable model of the learner (thinking style,
preferences, feedback), keeps a separate set of per-project preferences, and does
autonomous background research so that the next task is already prepared when the
learner sits down.

## Product shape in one paragraph

A user signs in with Google and creates **projects**. Each project holds an ordered list
of **tasks**, each with an estimated duration and a state. The user opens a task and
works with the coach agent in a streaming chat **session** — uploading work (text, PDF,
images), answering Socratic questions, getting exercises and reading/watching material.
A **scheduler** wakes the agent periodically; it finishes any interrupted background
work, then researches the next-up task in each idle project, posts a structured research
report into that task's session, and may add, split, or reorder tasks as a result.

## Scope for v1 (decided)

| Area | Decision |
| --- | --- |
| Tenancy | Multi-user. Per-user data isolation enforced server-side from day one. |
| Billing | Out of scope for v1. Data model reserves room for plans/quotas (see [02-data-model.md](02-data-model.md)). |
| Roles | Single role: project owner. No sharing, no orgs. |
| Research tooling | Gemini Google Search grounding + URL fetch + **YouTube Data API** for duration-accurate video selection. |
| Infrastructure | Terraform provisions GCP; GitHub Actions builds and deploys; shell scripts run the local dev loop. |

## The eight decisions that drive everything else

1. **Two data planes.** Domain data (projects, tasks, preferences) is app-owned and is
   the source of truth. ADK session/event data is conversation transcript only. The
   agent mutates domain data exclusively through typed tools that call the same
   repository layer the REST API uses. A chat transcript never *is* the task board.

2. **Sessions and memory persist to Firestore by extending ADK's shipped services.**
   `google-adk==2.7.0` ships `FirestoreSessionService` and `FirestoreMemoryService` for
   Python; we subclass them to add a per-session event `seq`, project/task linkage, and
   `get_user_state`, keeping transcript and domain data in one database under one
   authorization boundary. The version is deliberately pinned, since subclassing couples us
   to those classes' internals. See [03-agent-design.md](03-agent-design.md).

3. **Generation is owned by the process, not by the socket.** A turn runs in a detached
   asyncio task that writes checkpoints to Firestore and publishes chunks to an
   in-process broker. WebSockets subscribe to the broker. A client disconnect cannot
   cancel inference; a reconnect replays from checkpoint by sequence number. See
   [04-api-contract.md](04-api-contract.md).

4. **Autonomous work is a durable job ledger, not a cron script.** Cloud Scheduler pings
   the service, which enqueues per-project jobs to Cloud Tasks. Every run is a document
   with per-step status, so an interrupted run resumes at the failed step rather than
   restarting. See [05-autonomous-runs.md](05-autonomous-runs.md).

5. **Research output is a schema, not prose.** The agent emits a `ResearchReport` through
   a typed tool with explicit `required[]` and `optional[]` material lists and per-item
   minutes. The UI renders the two groups differently. This is the only reliable way to
   satisfy "must clearly distinguish what counts toward completion."

6. **Video duration is computed, not guessed.** `youtube.search.list` →
   `youtube.videos.list(part=contentDetails)` → parse ISO-8601 → filter against the
   task's remaining minute budget. The model picks *among* candidates that already fit.

7. **One auth path.** The browser never touches Firestore; no client-SDK path to it exists.
   The API service account is the only principal that can reach the data, which is an IAM
   *policy*, not a per-request lookup: the instance holds a cached access token (refreshed
   roughly hourly) and authorization is evaluated inside the Firestore RPC, so this adds no
   auth traffic to the request path. See
   [02-data-model.md](02-data-model.md#access-model). Live board updates arrive as
   invalidation messages multiplexed over the existing WebSocket, which TanStack Query turns
   into refetches. See [06-frontend.md](06-frontend.md).

8. **Manual and autonomous research are the same code path.**
   `POST /api/sessions/{session_id}/research` creates a run with `trigger: "manual"` and
   executes the identical workflow. No second implementation to keep in sync.

## Model configuration

Primary model: `gemini-3.7-flash`, via Vertex AI in production (IAM-based auth, no API
key to rotate) and the Gemini API for local development (fastest onboarding). One
`ModelConfig` abstraction, switched by env var.

Gemini 3.x changed generation config in ways that break older snippets — the plan assumes:

- `temperature`, `top_p`, `top_k` are **not** sent.
- `thinking_level` (`"low" | "medium" | "high"`) replaces `thinking_budget`. Default is
  `medium`. We use `low` for mechanical tool steps (task splitting, reordering) and
  `high` for research synthesis and the Socratic intake conversation.
- `candidate_count` is unsupported.
- Every `FunctionResponse` must carry `call_id` and `name`.

Multimodal: images and PDFs are passed as `types.Part` file references backed by GCS
through ADK's `GcsArtifactService`.

## Document map

| Doc | Contents |
| --- | --- |
| [01-architecture.md](01-architecture.md) | Components, request paths, repo layout, runtime topology |
| [02-data-model.md](02-data-model.md) | Firestore collections, task state machine, ordering, rollups |
| [03-agent-design.md](03-agent-design.md) | ADK agent graph, tools, custom Firestore services, learner model |
| [04-api-contract.md](04-api-contract.md) | REST endpoints, WebSocket protocol, resume semantics, auth |
| [05-autonomous-runs.md](05-autonomous-runs.md) | Scheduler, job ledger, leases, presence guard, recovery |
| [06-frontend.md](06-frontend.md) | Routes, TanStack Query vs Zustand split, board and session UI, theming |
| [07-infra-deploy.md](07-infra-deploy.md) | Terraform, Cloud Run settings, GitHub Actions, local dev |
| [08-testing.md](08-testing.md) | pytest/emulator, vitest, Playwright golden flows, agent evals |
| [09-roadmap.md](09-roadmap.md) | Milestones M0–M9 with exit criteria |
| [10-risks.md](10-risks.md) | Risks, mitigations, and open questions needing a decision |
