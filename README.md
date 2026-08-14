# Self-Study Coach

An adaptive agentic coach that turns your technical goals into bite-sized tasks, prepares
the material for the next one while you're away, and adjusts how it guides you as it
learns how you think.

**Status:** planning. No application code yet — the design is in [`docs/`](docs/).

## What it does

- Organizes work into **projects** and ordered, duration-estimated **tasks**; splits
  anything too big into bite-sized subtasks.
- Runs a **Socratic session** per task: understands uploaded work (text, PDF, images),
  asks rather than tells, and adapts to your preferences.
- Keeps a **learner model** across sessions plus **per-project preferences** — a 45-minute
  default task length globally, 2 hours in the project where you want it.
- Does **autonomous research** on a schedule: finds articles, exercises, and
  duration-checked videos for your next task, and clearly separates what's needed to
  finish the task from optional extras.
- Never interrupts you: background work skips any project you're currently working in.

## Stack

| Layer | Choice |
| --- | --- |
| Model | `gemini-3.7-flash` (Vertex AI in prod, Gemini API locally), multimodal |
| Agents | Google ADK — interactive coach + scheduled research workflow |
| Backend | Python 3.12, FastAPI, `uv`, `ruff`, WebSocket streaming, Cloud Run |
| Data | Firestore (domain + ADK sessions + memory), GCS for artifacts |
| Frontend | Vite, React, TypeScript, React Router, TanStack Query + Zustand, Tailwind + shadcn/ui — built into the API container and served at the same origin |
| Auth | Cloud Identity Platform, Google sign-in |
| Infra | Terraform, Cloud Scheduler + Cloud Tasks, GitHub Actions |
| Tests | pytest + gcloud Firestore emulator, Vitest, Playwright |

See [07-infra-deploy.md](docs/07-infra-deploy.md) for the toolchain a dev machine needs.

## Documentation

Start with [`docs/00-overview.md`](docs/00-overview.md) — it carries the scope decisions and
the eight design choices everything else follows from.

| Doc | Contents |
| --- | --- |
| [00-overview.md](docs/00-overview.md) | Product shape, v1 scope, key decisions, model config |
| [01-architecture.md](docs/01-architecture.md) | Components, layering, repo layout, request paths |
| [02-data-model.md](docs/02-data-model.md) | Firestore collections, task state machine, ordering, indexes |
| [03-agent-design.md](docs/03-agent-design.md) | ADK agent graph, tool catalogue, custom Firestore services, learner model |
| [04-api-contract.md](docs/04-api-contract.md) | REST + WebSocket protocol, auth, disconnect/resume semantics |
| [05-autonomous-runs.md](docs/05-autonomous-runs.md) | Scheduler, job ledger, leases, presence guard, recovery |
| [06-frontend.md](docs/06-frontend.md) | Routes, state split, board and workspace UI, theming |
| [07-infra-deploy.md](docs/07-infra-deploy.md) | Terraform, Cloud Run config, CI/CD, local dev |
| [08-testing.md](docs/08-testing.md) | Test strategy, contract suites, golden e2e flows |
| [09-roadmap.md](docs/09-roadmap.md) | Milestones M0–M7 with exit criteria |
| [10-risks.md](docs/10-risks.md) | Risks, mitigations, open questions |

## Next step

M0 in [the roadmap](docs/09-roadmap.md): scaffold both apps, provision dev infrastructure
with Terraform, and get a signed-in user rendered from a deployed Cloud Run service.
