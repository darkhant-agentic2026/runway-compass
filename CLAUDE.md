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

## Git: a branch per milestone, a commit once it is green

**Start a new milestone or other major goal on a fresh local branch off `main`**, before the
first edit rather than after the last one. Name it for the goal — `m2-sessions-streaming`,
`m3-coach-acts-on-the-board`, `adk-2.8-bump`. Nothing lands directly on `main`.

The reason is the shape of the work here: a milestone touches both apps, the Terraform, and
`docs/`, and it is only meaningfully verifiable at the end, when the disconnect matrix or the
golden flows run. A branch is what makes "this is half-finished" an ordinary state rather than
a broken `main`, and what makes abandoning an approach cost nothing.

**Commit only after the local gate passes, all of it:**

```
./scripts/dev.sh lint            # ruff --fix, ruff format, mypy, eslint --fix, tsc, terraform fmt
./scripts/dev.sh test            # api, then web, then e2e
```

Run the gate *again* after any change made in response to it, including a change that only
touches a comment or a doc — `dev.sh lint` rewrites files, so the tree that was green is not
necessarily the tree about to be committed. If something is still red, do not commit and say
so plainly; a red commit on a branch is not "saved work", it is a bisect trap.

Commit in the milestone's own vocabulary — what changed and why it was decided that way, not a
file list. Deviations from `docs/` belong in the roadmap's status section (see M0–M2 for the
form), not only in the commit message, because that is where the next person looks.

Pushing, opening a PR, and merging to `main` stay human steps. Ask before doing any of them.

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
- **`MODEL_BACKEND=stub` is refused for every `ENV` but `local`.** It is the deterministic
  model the e2e harness runs against. Like the dev-token path it is guarded by one check
  with a named regression test, and for a sharper reason: a deployed revision serving
  canned answers would reply, update the board, and look perfectly healthy.
  `docs/07-infra-deploy.md`
- **Auth is Cloud Identity Platform.** `firebase_admin` (Python) and `firebase/auth` (npm)
  are its client libraries — the same `identitytoolkit` service. Do not remove them as
  "leftovers" and do not introduce a Firebase project. `roles/firebaseauth.admin` is likewise
  the correct IAM role.
- **The SPA catch-all route registers last**, after every router, or it shadows `/api/*`,
  `/ws`, `/internal/*`, `/livez`, `/readyz`. `docs/07-infra-deploy.md`
- **Liveness is `/livez`, never `/healthz`.** Google's frontend intercepts `/healthz` on
  Cloud Run and answers it with its own 404 without forwarding to the container. Cloud
  Run's own probes bypass the frontend, so `/healthz` passes them and looks healthy
  everywhere a developer can see; only the deploy smoke test and the uptime check notice,
  and the uptime check fails silently. `tests/test_liveness_path.py`
- **Theme: the inline `index.html` script and `useThemeStore` share a `localStorage` key and
  format.** Do not route it through Zustand `persist` — the envelope breaks the inline
  reader. `docs/06-frontend.md`
- **Node 22**, matching the `node:22-slim` image pin.
- **`typescript` is pinned to `6.0.3`, not 7.x, and that is deliberate.** TypeScript 7's
  npm package exposes only `version` and `versionMajorMinor` — the JS compiler API is
  gone — so `typescript-eslint` cannot parse anything and `npm run lint` covers no `.ts`
  file at all. 6.0.3 is the last JS-based release. Re-check whether `typescript-eslint`
  supports 7 before bumping; the failure mode is a peer-dependency error that looks like
  a resolution problem rather than a missing API.
- **The Firestore emulator needs a JRE 21+**, and its floor rises with the Cloud SDK. Every
  backend test depends on the emulator. `docs/07-infra-deploy.md`
- **The emulator does not enforce index requirements; real Firestore does.** Any query
  filtering or ordering on more than one field needs an explicit `google_firestore_index`
  in `infra/terraform/modules/firestore/main.tf`, and any *collection-group* query needs
  one even for a single field — automatic single-field indexes are `COLLECTION`-scoped
  only. Without it the query passes every local test and returns `FAILED_PRECONDITION`
  the first time it runs deployed. Add the query, the index, and its row in
  `docs/02-data-model.md#indexes` in one change, or add the extra filter in Python
  instead — see `CoachSessionService.find_session_id_for_task`, which does the latter and
  has a test pinning its filter count.
- **An OAuth *scope* failure looks exactly like a missing IAM *role*.** IAM `signBlob`
  answers a storage-scoped token with `403 … ACCESS_TOKEN_SCOPE_INSUFFICIENT`, which
  reads as "the service account lacks permission" and is not — the binding can be
  present and correct. A client resolves ADC at whatever scope *it* needs, so a token
  borrowed from one API's client is rarely valid for another's. Resolve credentials for
  the call being made: `GcsObjectStore.SIGNING_SCOPES`.
- **A Zustand selector must never end in `?? []` or `?? {}`.** Zustand compares the
  selector's result with `Object.is`, so a fresh literal is a new value on every render and
  the component re-renders forever. React reports it as minified error #185 ("maximum
  update depth exceeded"), which names the symptom and not the cause, and it only shows up
  in a built bundle. Return a module-level frozen constant instead — see `NO_ATTACHMENTS`
  in `stores/composer.ts` and `DEFAULT_FILTERS` in `stores/boardUi.ts`.
- **Nothing in `ws/` may cancel generation.** `TurnRegistry` owns the task and a socket
  closing is a subscriber leaving. If `TurnService.start` ever grows an `await` on the
  generation task, or the task moves into a request handler's scope, the disconnect
  guarantee is gone and every test in the matrix still passes — just more slowly.
  `docs/04-api-contract.md#surviving-client-disconnects`

## Commands

`./scripts/dev.sh up | seed | tick | test [api|web|e2e] | lint` — see
`docs/07-infra-deploy.md` for what each does and for the machine prerequisites.

**Reach for `dev.sh` before reaching for the underlying tool.** `dev.sh test api` starts the
emulator and exports `ENV`, `GOOGLE_CLOUD_PROJECT`, and `FIRESTORE_EMULATOR_HOST` before
calling `uv run pytest`; a bare `uv run pytest` gets none of that and fails against a missing
emulator. Hand-rolling the environment inline — `ENV=local GOOGLE_CLOUD_PROJECT=… uv run
pytest` — also stalls on a permission prompt, because allow-list rules match a literal prefix
of the command string and the leading `VAR=value` assignments mean it no longer starts with
`uv`. Pass extra arguments through instead: `./scripts/dev.sh test api -q -k session`.
