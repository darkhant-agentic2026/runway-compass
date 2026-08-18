# Self-Study Coach

An adaptive agentic coach that turns your technical goals into bite-sized tasks, prepares
the material for the next one while you're away, and adjusts how it guides you as it
learns how you think.

**Status:** M0–M3 landed; M0–M2 are deployed and M3 is not yet verified on the dev
environment. A signed-in user can work the board by hand, hold a streaming conversation
with the coach about an uploaded screenshot, and have the coach *change the board* —
proposing a task list through a Socratic intake, splitting oversized work to fit the
project's own duration budget, and asking before it discards anything. Generation survives
a dropped socket and resumes across instances. CI builds, tests, and deploys on merge.

The design is in [`docs/`](docs/). [09-roadmap.md](docs/09-roadmap.md#status-after-m3)
records what is deferred, what was decided during implementation, and — worth reading
before writing anything — the failure modes a fully green local test run did not catch.

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

## Getting started

```bash
./scripts/dev.sh doctor    # check the machine prerequisites
./scripts/dev.sh up        # Firestore emulator + API + Vite
./scripts/dev.sh seed      # a demo user, project, and tasks
./scripts/dev.sh test      # api (pytest + emulator), web (vitest), e2e (Playwright)
./scripts/dev.sh lint      # ruff, mypy, eslint, tsc, terraform fmt
```

See [07-infra-deploy.md](docs/07-infra-deploy.md#prerequisites) for the toolchain, and
[infra/terraform/RUNBOOK.md](infra/terraform/RUNBOOK.md) for the two manual bootstrap
steps each environment needs before its first `terraform apply`.

## Next step

**M4** in [the roadmap](docs/09-roadmap.md#m4--research-15-weeks): research. A
`search_agent` behind an `AgentTool` hop, `fetch_url` with SSRF guards,
`youtube_find_by_duration` with real ISO-8601 duration filtering, and the `ResearchReport`
schema whose required/optional split is a product requirement rather than a rendering
choice.

Read [Status after M3](docs/09-roadmap.md#status-after-m3) first, and then the two tables
of failure modes it points at — [the M2 one](docs/09-roadmap.md#what-a-green-local-run-does-not-prove)
and [M3's three additions](docs/09-roadmap.md#three-more-rows-for-the-table-above). M4 is
the milestone that hits every row of both: new Firestore queries that need indexes the
emulator will not ask for, a *second* Google API whose credentials resolve at a different
scope than the first, and report events read back out of a transcript that keeps growing
after them.

One carry-over is worth doing before M4 rather than during it: **M3 has never run on
`coach-dev`.** Its agent tools, its intake session lookup, and its one new index have only
met the emulator and a stubbed model.
