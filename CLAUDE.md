# Self-Study Coach — working agreement

## Read the design before writing code

`docs/` holds a complete, decided design. Start at `docs/00-overview.md`, then read the doc
that owns the area you are touching (the index is in that file). Implement what is written
rather than re-deciding it. If two docs genuinely contradict each other, say so and stop —
do not silently pick one.

## Scope: local only

Build, test, and lint locally. **Never** run `terraform apply`/`destroy`, `gcloud auth`,
`gcloud run deploy`, or anything that touches a real GCP project. Firestore is the local
emulator. Cloud bootstrap (billing, Identity Platform, OAuth client, state bucket) is a human
step — see `docs/07-infra-deploy.md`.

A permission prompt for one of those commands means the work has drifted out of scope. Stop
and report rather than asking for approval.

## ADK: the installed source is the authority

**Important: `google-adk` is pinned at `2.7.0`. Do not bump it unless absolutely
necessary.** `adk_firestore/` *subclasses* ADK's shipped Firestore session and memory
services, so the coupling is to those classes' internals rather than to a stable abstract
interface — which is why the pin matters more here than it would elsewhere.

A bump is a deliberate task with its own verification pass, not routine maintenance. The
checklist of what to re-verify, and the reason each item is on it, lives in
`docs/03-agent-design.md#bumping-the-adk-version`. Read it before touching the version;
don't reconstruct it from memory.

Two habits keep that surface accurate:

- For any ADK signature or semantic, read the **installed package source** under `apps/api`'s
  virtualenv (`.venv/lib/python3.12/site-packages/google/adk/**`). It matches the pinned
  version exactly, which published documentation may not.
- ADK ships SDKs for Python, Java, Kotlin, Go, and TypeScript, and `adk.dev` indexes them in
  one namespace. Confirm a page is about Python before applying it — notably
  `adk.dev/integrations/firestore-session-service/` documents the **Java**
  `com.google.adk.sessions.FirestoreSessionService`, a different class from the Python one of
  the same name that we subclass.

Pin all other dependencies too.

## Layering

From `docs/01-architecture.md`:

> **Agent tools call `services/`, never `repositories/` directly.**

`repositories/` is the only module that knows Firestore collection paths. `services/` holds
authorization, rollup maintenance, and event publication, so the agent and the REST API
cannot diverge. A tool completing a task calls the same `TaskService.complete_task()` the
user's button calls.

## Footguns established during design — do not "fix" these

- **`ENV=local` accepts `Authorization: Bearer dev:<uid>`.** This is deliberate auth-bypass
  code standing in for an emulator that does not exist. Its test asserting the path is inert
  for every other `ENV` must never be deleted. `docs/04-api-contract.md`
- **Auth is Cloud Identity Platform.** `firebase_admin` (Python) and `firebase/auth` (npm)
  are its client libraries — the same `identitytoolkit` service. Do not remove them as
  "leftovers" and do not introduce a Firebase project. `roles/firebaseauth.admin` is likewise
  the correct IAM role.
- **The SPA catch-all route registers last**, after every router, or it shadows `/api/*`,
  `/ws`, `/internal/*`, `/healthz`, `/readyz`. `docs/07-infra-deploy.md`
- **Theme: the inline `index.html` script and `useThemeStore` share a `localStorage` key and
  format.** Do not route it through Zustand `persist` — the envelope breaks the inline
  reader. `docs/06-frontend.md`
- **Node 22**, matching the `node:22-slim` image pin.
- **The Firestore emulator needs a JRE 21+**, and its floor rises with the Cloud SDK. Every
  backend test depends on the emulator. `docs/07-infra-deploy.md`

## Commands

`./scripts/dev.sh up | seed | tick | test [api|web|e2e] | lint` — see
`docs/07-infra-deploy.md` for what each does and for the machine prerequisites.
