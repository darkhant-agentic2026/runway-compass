# Infrastructure, Deployment & Local Dev

Terraform provisions GCP. GitHub Actions builds and deploys. Shell scripts run the local
loop.

## Environments

Two GCP projects, `coach-dev` and `coach-prod`, identical Terraform with different
`tfvars`. Separate Firestore databases, buckets, and service accounts; no shared state.
Terraform state in a GCS backend bucket per environment with versioning and object locking.

## Terraform layout

```
infra/terraform/
├── modules/
│   ├── cloud_run_service/    # service, IAM, env, secrets, probes
│   ├── firestore/            # database, indexes, TTL policies
│   ├── scheduler_tasks/      # Cloud Scheduler job + Cloud Tasks queue + SAs
│   ├── storage/              # artifact + upload buckets, CORS, lifecycle
│   ├── identity/             # service accounts, WIF pool, Identity Platform
│   └── observability/        # log sinks, alert policies, dashboards, uptime checks
└── envs/
    ├── dev/{main.tf,dev.tfvars,backend.tf}
    └── prod/{main.tf,prod.tfvars,backend.tf}
```

### Resources provisioned

| Resource | Notes |
| --- | --- |
| `google_project_service` × N | `run`, `firestore`, `cloudtasks`, `cloudscheduler`, `aiplatform`, `artifactregistry`, `secretmanager`, `storage`, `identitytoolkit`, `monitoring`, `logging`, `cloudtrace`; the **three separate IAM-family APIs** (`iam` for service accounts and the WIF pool, `iamcredentials` for SignBlob on the upload URLs, `cloudresourcemanager` for `projects.setIamPolicy`) plus `sts` for the GitHub OIDC exchange; and **`youtube.googleapis.com`** — the YouTube Data API is easy to forget because it is the one dependency reached with an API key rather than IAM, so nothing else in the stack references it |
| `google_cloud_run_v2_service.coach_api` | Settings below |
| `google_firestore_database` | Native mode, `us-central1`, PITR on in prod |
| `google_firestore_index` × N | From [02-data-model.md](02-data-model.md) |
| `google_firestore_field` TTL | `turns.endedAt` (7 d), `autonomous_runs.updatedAt` (30 d) |
| `google_storage_bucket` × 2 | `-coach-artifacts` (uniform access, CMEK-ready), `-coach-uploads` (CORS for signed PUT, 1-day lifecycle on unfinalized) |
| `google_cloud_scheduler_job.tick` | `*/15 * * * *`, OIDC token for the scheduler SA |
| `google_cloud_tasks_queue.autonomous_runs` | `max_dispatches_per_second = 1`, `max_concurrent_dispatches = 5`, retry 3× with 30 s–10 min backoff |
| `google_secret_manager_secret` | `youtube-api-key`, `gemini-api-key` (dev only) |
| Service accounts | `coach-api-sa`, `coach-scheduler-sa`, `coach-tasks-sa`, `github-deployer-sa` |
| `google_iam_workload_identity_pool` | Keyless GitHub Actions → GCP auth |
| `google_identity_platform_config` | Authorized domains = the Cloud Run service URL (+ custom domain in prod) |
| `google_identity_platform_default_supported_idp_config` | `idp_id = "google.com"`, client ID/secret from the OAuth client below |
| Monitoring | Alert policies from [05-autonomous-runs.md](05-autonomous-runs.md), uptime check on `/livez`, log-based error metric |

The SPA is served by the Cloud Run service itself (see [Container](#container)), so there is
no hosting resource here.

### Manual bootstrap steps (two, both one-time per environment)

Everything else is `terraform apply` from zero. These two are the caveat on the M0 exit
criterion in [09-roadmap.md](09-roadmap.md) and belong in the environment runbook, not in
tribal memory:

1. **Enable Identity Platform via the Cloud Marketplace.** The provider docs for
   `google_identity_platform_default_supported_idp_config` state the product must be enabled
   in the marketplace before the resource can be used, and
   `google_identity_platform_config` additionally requires a billing-enabled project.
   Whether `google_project_service.identitytoolkit` alone satisfies this is worth testing
   first at M0 — if it does, this step disappears.
2. **Create the OAuth 2.0 web client and consent screen** (APIs & Services → Credentials).
   Not cleanly Terraformable; put the secret in Secret Manager and pass the client ID in as
   a tfvar. Re-check whether `google_oauth_client` has stabilised before accepting this
   permanently.

Both `google_identity_platform_*` resources are in the GA `hashicorp/google` provider, so the
stack needs no `google-beta` dependency.

`google_identity_platform_config` exports `client.api_key` and `client.firebase_subdomain`,
so the Web API key and auth domain the SPA needs are Terraform *outputs* rather than values
copied by hand into CI. `client.api_key` lands in state as plain text; the state bucket is
access-controlled and versioned (see [Environments](#environments)), and the value is public
by design.

### IAM (least privilege)

| SA | Roles |
| --- | --- |
| `coach-api-sa` | `datastore.user`, `storage.objectAdmin` (2 buckets), `aiplatform.user`, `cloudtasks.enqueuer`, `secretmanager.secretAccessor`, `firebaseauth.admin` (see note), `logging.logWriter`, `cloudtrace.agent`, plus the two below |
| ” — `iam.serviceAccountTokenCreator` **on itself** | V4 signed upload URLs are signed through the IAM SignBlob API rather than a downloaded key. Without this the upload flow fails at runtime with a signing error, and only in a deployed environment — local dev uses the developer's impersonated credentials, so it passes there |
| ” — `iam.serviceAccountUser` **on `coach-tasks-sa`** | Creating a Cloud Task that carries an OIDC token as `coach-tasks-sa` requires acting as that account |
| `coach-scheduler-sa` | `run.invoker` on the service (OIDC audience = service URL) |
| `coach-tasks-sa` | `run.invoker` on the service |
| `github-deployer-sa` | `run.admin`, `artifactregistry.writer`, `iam.serviceAccountUser` — enough to build, push, and deploy an image, and deliberately not enough to apply Terraform ([why](#ci-does-not-run-terraform)) |

`roles/firebaseauth.admin` is the IAM role governing Identity Platform; `coach-api-sa` needs
it so the `DELETE /api/me` cascade can remove the identity record. Token *verification* needs
no IAM at all — it is an offline signature check against Google's public keys.

`datastore.user` on `coach-api-sa` is the entire Firestore access boundary: no other
principal can read the data, and no client-side path exists
([02-data-model.md](02-data-model.md#access-model)).

No service account keys anywhere. GitHub Actions authenticates via Workload Identity
Federation.

## Cloud Run configuration (the settings that matter)

```hcl
scaling {
  min_instance_count = 1        # a warm instance; also avoids scale-to-zero mid-generation
  max_instance_count = 10
}
template {
  timeout                          = "3600s"   # long WS connections and long turns
  max_instance_request_concurrency = 40        # WS connections are cheap; agent runs are not
  session_affinity                 = true      # reconnects prefer the owning instance
  containers {
    resources {
      limits         = { cpu = "2", memory = "2Gi" }
      cpu_idle       = false       # CPU ALWAYS ALLOCATED — required
      startup_cpu_boost = true
    }
  }
}
```

`cpu_idle = false` is not an optimization, it is a correctness requirement: with
request-based CPU allocation, a container is throttled to near-zero CPU outside of request
processing, which would stall a detached generation task the moment its client
disconnects — exactly the scenario the design must survive.

`min_instance_count = 1` costs a little idle money and buys: no cold start on the
scheduler tick, warm ADK/Vertex clients, and a stable instance for session affinity.

**It is a per-environment variable (`min_instances`), defaulting to 1, and `coach-dev`
currently sets it to 0.** That is a deliberate, temporary trade recorded in
`envs/dev/dev.tfvars`: through M1 there is no streaming to lose, and an idle dev
environment that holds no instance bills essentially nothing. Note that `cpu_idle = false`
is unaffected — CPU stays allocated for an instance's whole lifetime; what is given up is
the *warm* instance, so the first request after a quiet period pays a cold start.

**This must return to 1 in every environment before M2.** From M2 the setting stops being
a cost preference: a scaled-to-zero instance can be reaped in the middle of a detached
generation task, which is exactly the failure
[04-api-contract.md](04-api-contract.md#surviving-client-disconnects) is built to survive.
It is called out here, and in the variable's own description, because a dev environment
that quietly disagrees with this document is how that gets discovered by debugging a
dropped stream instead of by reading.

A per-instance `asyncio.Semaphore` caps concurrent agent runs (default 8) so a burst of
background work cannot starve interactive turns; excess background work stays queued in
Cloud Tasks where it belongs.

## Container

One image contains both apps: `./Dockerfile` at the repo root (not under `apps/api/`, since
the build context spans both). Multi-stage, distroless-ish runtime:

```dockerfile
FROM node:22-slim AS web
WORKDIR /w
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build                      # → /w/dist

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY apps/api/src/ src/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 coach
WORKDIR /app                           # REQUIRED: main.py resolves static/ relatively
COPY --from=builder --chown=coach:coach /app /app
COPY --from=web --chown=coach:coach /w/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER coach
CMD ["uvicorn", "coach.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

Two things in that last stage are load-bearing:

- **`WORKDIR /app` is not cosmetic.** `main.py` mounts `StaticFiles(directory="static/assets")`
  and returns `FileResponse("static/index.html")` — both relative. Without the `WORKDIR` the
  process starts in `/`, the mount raises at import time, and the image fails on first boot
  rather than in CI.
- **There is no `nonroot` user in `python:3.12-slim`.** That account comes from distroless
  base images; Debian slim has `nobody` but no home directory. The account must be created
  explicitly or `USER` fails the build.

Shipping the SPA in the API image gives a single origin for free and makes SPA/API version
skew structurally impossible — there is one artifact and one revision, so a rollback rolls
back both halves together.

In `coach/main.py` the static mount and the SPA fallback are registered **last**, after
every router, so the catch-all cannot shadow `/api/*`, `/ws`, `/internal/*`, `/livez`, or
`/readyz`:

```python
app.mount("/assets", StaticFiles(directory="static/assets"), name="assets")

@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str) -> FileResponse:
    return FileResponse("static/index.html")
```

Route-ordering is a real footgun here, so it gets an explicit test asserting that every
known API prefix still resolves to its handler and not to `index.html`.

Single uvicorn worker per instance — the in-process `TurnRegistry` and `StreamBroker`
assume one process. Horizontal scaling is Cloud Run's job; the Firestore replay path
handles cross-instance reconnects.

## GitHub Actions

### `ci.yml` (every PR)

```
api:  uv sync → ruff check → ruff format --check → mypy → pytest (Firestore emulator service)
web:  npm ci → tsc --noEmit → eslint → vitest run --coverage → vite build
e2e:  docker compose (single app image + Firestore emulator) → playwright test
tf:   terraform fmt -check → terraform validate → tflint
```

The `web:` job still runs `vite build`, but only as a typecheck-and-lint gate. The
*deployable* bundle is the one produced inside the Docker build, so there is exactly one
build of the SPA that can ever reach production.

### `deploy-cloudrun.yml`

Trigger: push to `main` (dev) or a `v*` tag / manual dispatch (prod, with a GitHub
Environment protection rule requiring approval).

```
1. auth to GCP via Workload Identity Federation (no keys)
2. docker build + push to Artifact Registry, tagged with the commit SHA
3. gcloud run deploy --image …:$SHA --no-traffic --tag=candidate
3. smoke test the candidate revision URL: /livez, /readyz, one authenticated round-trip
5. gcloud run services update-traffic --to-tags candidate=100   (or 10 → 100 canary in prod)
6. on smoke-test failure: leave traffic on the previous revision, fail loudly
```

#### CI does not run Terraform

An earlier revision of this document put `terraform plan → terraform apply` between steps
2 and 3, and that contradicted the [IAM table](#iam-least-privilege) two sections down,
which grants `github-deployer-sa` only `run.admin`, `artifactregistry.writer`, and
`iam.serviceAccountUser`. Those roles deploy an image; they come nowhere near applying
this stack, which creates service accounts, project IAM bindings, a Firestore database,
and Secret Manager secrets. Reconciling the two would have meant either granting CI
near-admin over the project plus write access to the state bucket, or dropping the apply.

**Resolved in favour of the IAM table.** The deployer's roles are unchanged, it has no
access to the state bucket, and infrastructure changes are an operator action run from
[infra/terraform/RUNBOOK.md](../infra/terraform/RUNBOOK.md).

What that buys: a bad merge can ship a bad image — caught by the smoke test at step 4 and
reversed by `update-traffic` in seconds — and cannot rewrite IAM or delete the Firestore
database. What it costs: nothing detects infrastructure drift on the deploy path, which is
what the nightly Terraform plan check in [08-testing.md](08-testing.md#ci-wiring) is for.
That job can hold read credentials because it is not triggered by a merge.

The consequence worth remembering is that **the running image is chosen by CI, not by
Terraform.** `var.image` seeds the service at creation and the Cloud Run module then
ignores changes to it, so an operator applying an unrelated change cannot silently roll
the service back to whatever tag was last in their shell.

Rollback is `update-traffic --to-revisions <previous>=100`; revisions are immutable and
retained — and because the SPA lives in the same image, a rollback reverts the frontend
with it.

PR previews are the `--tag=candidate` revision URLs produced at step 4, which serve the SPA
and API of that commit together.

Vite needs the environment's Identity Platform config at build time, so those are build args
on the `web` stage, read from the Terraform outputs described above
(`client.api_key`, `client.firebase_subdomain`) plus the OAuth client ID. They are public
values by design — the security boundary is the authorized-domains list and server-side
token verification, not secrecy of the API key.

### Migrations

Firestore is schemaless, but shape changes still need care. `apps/api/migrations/` holds
numbered idempotent scripts run as a one-shot Cloud Run Job after deploy; a
`schemaVersion` field on affected documents makes each script resumable and re-runnable.

## Local development

### Prerequisites

| Tool | Why | Install |
| --- | --- | --- |
| Python 3.12 | `apps/api` | system or `uv python install 3.12` |
| `uv` | Python deps, lockfile, task running | astral installer |
| Node 22 + npm | `apps/web`; matches the `node:22-slim` image pin | `nvm install 22 && nvm alias default 22` |
| Docker + compose | image build, e2e harness | distro package |
| Terraform | `fmt` / `validate` locally, `apply` in CI | static binary to `~/.local/bin` |
| `tflint` | the `tf:` CI job | static binary |
| `gcloud` | emulator, deploys | Cloud SDK |
| `gcloud` component `beta` | `gcloud beta emulators firestore` | `gcloud components install beta` |
| `gcloud` component `cloud-firestore-emulator` | every backend test | `gcloud components install cloud-firestore-emulator` |
| **A JRE (see the floor note below)** | the Firestore emulator is a Java jar, and the Cloud SDK bundles Python but **no** JRE | a JRE on `PATH`; Temurin is the usual choice |
| `unzip` | Terraform and tflint ship as `.zip` | distro package |
| `gh` | optional; only for repo/WIF setup | static binary |

`nvm alias default 22` matters as much as `nvm use 22`: `use` affects only the current
shell, so without the alias a new shell — or a restarted editor — silently reverts to
whatever was default and you get a Node version that disagrees with the Dockerfile.

**The emulator's minimum JRE tracks the Cloud SDK and rises over time**, and every backend
test needs the emulator. Re-check `java -version` against the emulator's current floor after
a `gcloud components update`.

The floor as observed, so the next person can tell drift from breakage:

| Date | Cloud SDK | Emulator's stated requirement | Installed JRE | Result |
| --- | --- | --- | --- | --- |
| 2026-08-14 | 580.0.0 | JRE 25+ (see the warning below) | Temurin 21.0.12 | **Warns, then starts and works.** All 177 backend tests pass. |

The warning, verbatim, is what `./scripts/dev.sh` prints on a cold emulator start:

```
WARNING: Cloud Firestore Emulator support for Java JRE version 21 will be dropped after
gcloud command-line tool release 576.0.0. Please upgrade to Java JRE version 25 or higher
to continue using the latest Cloud Firestore Emulator.
```

Note the tense: support "will be dropped **after** 576.0.0", and this box is on 580.0.0
and still works — so the emulator is warning ahead of enforcing, not describing something
that has already happened. That gap is the window in which to upgrade.

Two consequences of that row:

- The warning is the emulator's own text, not ours. `./scripts/dev.sh` runs a `java -version`
  preflight and **surfaces the emulator's warning rather than suppressing or gating on it** —
  the point is to make the drift visible on the day it starts mattering, not to block work
  today.
- Treat "JRE 21+" as a snapshot, not a contract. The emulator has historically warned for a
  while before it actually refuses to start; when it does refuse, it refuses outright rather
  than degrading, so the failure mode is a hard stop on every backend test at once. Bumping
  the dev-machine and CI-runner JRE is the fix, and it is cheap — it is only listed as a
  prerequisite here because nothing else in the stack needs Java at all.

### Python dependency pins that are not routine

`apps/api/pyproject.toml` pins everything exactly (`==`) and `uv.lock` is committed. Three
of those pins carry a reason beyond "reproducibility":

| Dependency | Pin | Why it is called out |
| --- | --- | --- |
| `google-adk` | `==2.7.0` | Deliberate, and not routine to bump — `adk_firestore/` subclasses the shipped Firestore session/memory pair, so the coupling is to their internals. Checklist: [03-agent-design.md](03-agent-design.md#bumping-the-adk-version) |
| **`google-cloud-firestore`** | `==2.28.1` | **A direct dependency of ours, not something `google-adk` drags in.** See below |
| `firebase-admin` | `==7.5.0` | The Admin SDK for `identitytoolkit`; it is what `verify_id_token` lives in. Not a Firebase-project artifact and not removable as a leftover ([04-api-contract.md](04-api-contract.md#authentication)) |

**`google-cloud-firestore` must be declared explicitly.** `google-adk==2.7.0` lists it only
under the `all`, `extensions`, and `test` **extras** — the base install does not pull it in:

```
$ importlib.metadata.requires("google-adk") | grep firestore
google-cloud-firestore>=2.11,<3 ; extra == "all"
google-cloud-firestore>=2.11,<3 ; extra == "extensions"
google-cloud-firestore>=2.11,<3 ; extra == "test"
```

So `pip install google-adk==2.7.0` alone gives an install where
`from google.adk.integrations.firestore.firestore_session_service import FirestoreSessionService`
raises `ImportError` — the exact import [03-agent-design.md](03-agent-design.md) is built on.
Depending on `google-adk[extensions]` instead would work but would also pull the rest of that
extra, and would leave the version of the one package this project subclasses against being
chosen by someone else's range. An explicit `==` pin is the smaller surface.

**Playwright browsers.** Chromium and WebKit are both installed and both run in CI. Chromium alone was enough through M1. WebKit is first needed at **M2**,
where golden flow #4 (disconnect and resume) becomes an exit criterion. Install it with
`npx playwright install --with-deps webkit`; the `--with-deps` step needs sudo, so it is
worth doing at the same time as the JRE rather than hitting a privilege prompt mid-milestone.
WebKit is not optional coverage here — see [08-testing.md](08-testing.md).

Note the split: the browser download needs no privileges, only `--with-deps` does. So a
Playwright bump that pulls a WebKit build requiring *new* system libraries fails at launch
with a missing-shared-object error rather than at install time.

`scripts/dev.sh` orchestrates:

| Command | Effect |
| --- | --- |
| `./scripts/dev.sh up` | `gcloud beta emulators firestore start`, API with `--reload`, Vite dev server |
| `./scripts/dev.sh seed` | Seeds a demo user, project, and 8 tasks |
| `./scripts/dev.sh tick [--loop 60s]` | Calls `/internal/tick` locally (OIDC bypassed when `ENV=local`) |
| `./scripts/dev.sh test [api\|web\|e2e]` | Test subsets |
| `./scripts/dev.sh lint` | `ruff check --fix`, `ruff format`, `eslint --fix`, `tsc` |

Local model access uses the **Gemini API** with a developer key in `.env.local` (fastest
onboarding); production uses **Vertex AI** with the Cloud Run service account (no key to
rotate, VPC-SC compatible, and per-project quotas). One `ModelProvider` abstraction,
selected by `MODEL_BACKEND=gemini_api|vertex`. Cloud Tasks is stubbed by an in-process
`JobQueue` implementation behind the same interface, so the full autonomous path runs on a
laptop.

### The two local dependencies that are not emulated

**Auth.** Identity Platform has no local emulator, so `ENV=local` enables the
`Bearer dev:<uid>` path described in
[04-api-contract.md](04-api-contract.md#authentication). It is fast and deterministic but
does not exercise real token verification; a nightly job covers that gap against `coach-dev`
([08-testing.md](08-testing.md)).

**Storage.** Uploads use V4 signed URLs, which need a real signer. Local dev points at a real
`coach-dev` bucket and developers authenticate with:

```
gcloud auth application-default login \
  --impersonate-service-account=coach-api-sa@coach-dev.iam.gserviceaccount.com
```

Signing then works through the IAM SignBlob API, which keeps the no-service-account-keys
rule intact. Developers need `roles/iam.serviceAccountTokenCreator` on `coach-api-sa` in
dev only. Unit tests fake the artifact service outright and touch neither GCS nor a signer.

`.env` contract (all validated at startup by a Pydantic `Settings` model, fail-fast):

```
ENV=local|dev|prod
GOOGLE_CLOUD_PROJECT=…
MODEL_BACKEND=vertex                          # vertex | gemini_api
MODEL_NAME=gemini-3.7-flash
VERTEX_LOCATION=us-central1
GEMINI_API_KEY=…                              # required iff MODEL_BACKEND=gemini_api (local)
FIRESTORE_DATABASE=(default)
FIRESTORE_EMULATOR_HOST=localhost:8081        # local only; unset in dev/prod
ADK_FIRESTORE_ROOT_COLLECTION=adk-session     # pinned explicitly, not left to the ADK default
ARTIFACT_BUCKET=…
UPLOAD_BUCKET=…
TASKS_QUEUE=projects/…/locations/…/queues/autonomous-runs
TASKS_TARGET_URL=https://…/internal/runs
TASKS_INVOKER_SA=coach-tasks-sa@….iam.gserviceaccount.com   # OIDC identity minted onto each task
YOUTUBE_API_KEY=sm://youtube-api-key
ALLOWED_SCHEDULER_SA=coach-scheduler-sa@….iam.gserviceaccount.com
ALLOWED_TASKS_SA=coach-tasks-sa@….iam.gserviceaccount.com   # /internal/runs/* caller; distinct from the above
OAUTH_CLIENT_ID=….apps.googleusercontent.com  # Identity Platform Google provider; token audience
```

`/internal/tick` and `/internal/runs/{id}/execute` are invoked by **two different** service
accounts, so OIDC verification needs both allow-listed separately
([04-api-contract.md](04-api-contract.md#authentication)). Collapsing them into one variable
would mean either Cloud Scheduler could invoke the executor or the reverse.

`Settings` rejects a non-`local` `ENV` combined with `FIRESTORE_EMULATOR_HOST` — a
fail-fast guard against a deployed revision silently pointing at nothing.

## Cost notes

Dominant costs: Gemini tokens (research turns with grounding are the expensive ones),
Cloud Run `min-instances=1` with CPU always allocated, and Firestore writes from event
and checkpoint persistence. Mitigations already in the design: checkpoint batching
(400 ms / 512 chars rather than per-token writes), a 6-hour per-project autonomous
cooldown, a 24-hour YouTube result cache, `thinking_level: low` for mechanical steps, and
per-user daily run and token caps. A `usage/{uid}_{date}` counter makes actual spend
visible before it becomes a surprise, and is the same counter billing would later meter.
