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
./scripts/dev.sh lint            # ruff --fix, ruff format, mypy, eslint --fix, prettier --write, tsc, terraform fmt
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
- **Two more test-only surfaces exist on the same terms**, each behind one `settings.is_local`
  check with a named regression test for every other `ENV`: `MODEL_BACKEND=stub`, the
  deterministic model the e2e harness runs against (`docs/07-infra-deploy.md`), and
  `api/routers/local_storage.py`, which receives the signed-upload PUT so the upload path is
  reachable from a browser at all (`docs/08-testing.md`). Both fail *silently* if the guard
  goes: a revision serving canned answers looks perfectly healthy, and an unauthenticated
  PUT endpoint is an open door.
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
- **A Google client is built lazily, but never behind a proxy if something else
  type-checks it.** `LazyProxy` defers a client we only call methods on. The artifact
  service is not one: ADK puts it on an `InvocationContext`, whose field pydantic
  validates with `isinstance`, so the proxy broke every deployed turn while every local
  test passed. Defer that kind with a provider — a callable resolved at first use, so the
  library gets the real object. `integrations/artifacts.py`, `coach/core/lazy.py`
- **Node 22**, matching the `node:22-slim` image pin.
- **`typescript` is pinned to `6.0.3`, not 7.x, and that is deliberate.** TypeScript 7's
  npm package exposes only `version` and `versionMajorMinor` — the JS compiler API is
  gone — so `typescript-eslint` cannot parse anything and `npm run lint` covers no `.ts`
  file at all. 6.0.3 is the last JS-based release. Re-check whether `typescript-eslint`
  supports 7 before bumping; the failure mode is a peer-dependency error that looks like
  a resolution problem rather than a missing API.
- **The Firestore emulator needs a JRE 21+**, and its floor rises with the Cloud SDK. Every
  backend test depends on the emulator. `docs/07-infra-deploy.md`
- **A Zustand selector must never end in `?? []` or `?? {}`.** Zustand compares the
  selector's result with `Object.is`, so a fresh literal is a new value on every render and
  the component re-renders forever. React reports it as minified error #185 ("maximum
  update depth exceeded"), which names the symptom and not the cause, and it only shows up
  in a built bundle. Return a module-level frozen constant instead — see `NO_ATTACHMENTS`
  in `stores/composer.ts` and `DEFAULT_FILTERS` in `stores/boardUi.ts`.
- **A placeholder in the coach's instruction is only as good as its writer.**
  `inject_session_state` raises `KeyError` on a `{key}` with no state key, while
  assembling the LLM request — inside the detached generation task, on the first real turn
  of a deployed revision. `tests/test_agent_prompt.py` reads the template and asserts every
  placeholder has a writer; keep it reading the template rather than restating the list.
- **`board_update` is *registered* on the socket, never passed to `getSocket`.**
  `getSocket(deps)` builds the singleton on the first call and ignores its arguments
  afterwards, and React runs child effects before parent ones — so a screen that reaches
  for the socket can silently win the race against `AppShell` and leave the board never
  refreshing. Nothing about that fails a test that starts on the board.
  `docs/06-frontend.md#the-bridge`
- **Nothing in `ws/` may cancel generation.** `TurnRegistry` owns the task and a socket
  closing is a subscriber leaving. If `TurnService.start` ever grows an `await` on the
  generation task, or the task moves into a request handler's scope, the disconnect
  guarantee is gone and every test in the matrix still passes — just more slowly.
  `docs/04-api-contract.md#surviving-client-disconnects`

## A green local gate is weaker evidence than it looks

Seven of the nine defects fixed while closing M2 were invisible to a fully passing local
run, and M3 added three more rows plus two defects that a *user* found after the gate went
green. The failure modes, what each looks like, and where each is likely to recur are
tabulated in `docs/09-roadmap.md#what-a-green-local-run-does-not-prove` and continued in
`docs/09-roadmap.md#three-more-rows-for-the-table-above`. **Read both before writing
anything that queries Firestore, calls a second Google API, or reads a stored ADK event.**

Four working habits follow, and none belongs in `docs/`:

- **When a fixture stands in for a shape this project does not define, generate it.**
  Hand-written fixtures encode the same assumption as the code they test, so both are wrong
  together and the suite is green. `scripts/gen_event_vectors.py` and
  `scripts/gen_ordering_vectors.py` are the pattern; add one rather than inventing a
  payload.
- **Prefer a test that pins a *decision* over one that pins a result** where the two
  differ. A composite Firestore query and a single-field one return the same rows locally,
  and only one of them works deployed; the same is true of a signed URL's arguments. In
  those cases assert the call, not the output.
- **When a fix has two mechanisms, disable each one and re-run the test.** A test that
  passes is not evidence about the mechanism you think it is testing: M3's board-refresh
  fix had a push *and* a fallback, and only turning each off in turn showed which one the
  regression test was actually exercising. Two five-minute runs, and the alternative is a
  test that keeps passing after the interesting half is deleted.
- **When ADK's behaviour is the question, dump it — do not reason about it.** A throwaway
  pytest that prints the stored events answered "where does the confirmation request sit
  in the transcript" and "what shape is a function response" in one run, after two wrong
  guesses. Delete the probe afterwards; the answer belongs in a vector or a test.
- **Run the e2e suite several times before believing it, and diagnose a flake rather than
  padding a timeout.** Two specs that had passed since M2 failed roughly one run in three.
  One was genuinely a timing race; the other looked like one, failed only on the mobile
  projects, and turned out to be a real UI defect — the page scrolling under the click.
  Reproduce it against a manually started stack (`docker compose -f docker-compose.e2e.yml
  up --build -d --wait`, then `npx playwright test <spec> --project=<one>` in a loop): a
  five-second run you can repeat twenty times beats a five-minute suite you can repeat
  twice. Instrument the app if the cause is not visible from outside — a temporary
  `console.log` plus `page.on('console')` answered in one run what two rounds of reasoning
  got wrong. Rebuild the image after editing the app; the e2e stack serves a built SPA.

## Reporting a deployed failure

`docs/07-infra-deploy.md` covers the deploy itself; `infra/terraform/RUNBOOK.md` §8 and §9
are the manual verifications for M2 and M3, and a milestone gets one when it has a surface
no local test can reach. What is worth knowing when something fails there:

- Ask for the **server-side traceback**, not the browser's status code. Logs are JSON with
  the traceback under `jsonPayload.exception`, so `gcloud run services logs read | grep` is
  the wrong instrument — use
  `gcloud logging read 'resource.type="cloud_run_revision" AND severity>=ERROR' --format='value(jsonPayload.exception)'`.
- A 500 response now carries a `traceId` matching `gcloud logging read 'trace:"…"'`, so a
  screenshot is enough to find the line.
- Before proposing a fix from an error message, check the message is *about* what it names.
  Two M2 diagnoses went the wrong way on this: a 403 naming an IAM method that was really an
  OAuth scope, and a probe that 404'd for every input because the method it called did not
  exist — which read as "no models are available".

## Commands

`./scripts/dev.sh up | seed | tick | test [api|web|e2e] | lint | doctor` — see
`docs/07-infra-deploy.md` for what each does and for the machine prerequisites. Two more
regenerate committed cross-language fixtures: `gen-ordering-vectors` and
`gen-event-vectors`.

**`dev.sh test api` is a *weaker* gate than CI in one respect**: it exports
`FIRESTORE_EMULATOR_HOST` before pytest, so anything resolved at import time gets an
anonymous Firestore client. CI has no such variable until a fixture starts the emulator,
which is after collection. It also usually has ADC, which CI does not. Keep cloud-client construction out of
constructors — `coach/core/lazy.py` explains why and
`tests/test_import_without_credentials.py` pins it.

**Prettier formats `apps/web`; ESLint only lints it.** Both run from `dev.sh lint`, and
the order there is deliberate: `eslint --fix` first, `prettier --write` second, because a
fixer rewrites code and the formatter has to be the last thing that touches it.
`eslint-config-prettier` is the last entry in `eslint.config.js`, so no rule has an opinion
about layout; do not add `eslint-plugin-prettier`. **Import order is Prettier's too** —
`@ianvs/prettier-plugin-sort-imports`, which is only safe as a formatter because it never
moves an import across a side-effect import; `prettier-plugin-tailwindcss` must stay last
in the plugin list. **Prettier's remit stops at
`apps/web`** — `.prettierignore` excludes `docs/`, every other `*.md`, `infra/`, and
`apps/api/`, whose prose is hand-wrapped and whose tables are aligned for reading. Python
formatting is still `ruff format`. `docs/07-infra-deploy.md#formatting-and-linting`

**Reach for `dev.sh` before reaching for the underlying tool.** `dev.sh test api` starts the
emulator and exports `ENV`, `GOOGLE_CLOUD_PROJECT`, and `FIRESTORE_EMULATOR_HOST` before
calling `uv run pytest`; a bare `uv run pytest` gets none of that and fails against a missing
emulator. Hand-rolling the environment inline — `ENV=local GOOGLE_CLOUD_PROJECT=… uv run
pytest` — also stalls on a permission prompt, because allow-list rules match a literal prefix
of the command string and the leading `VAR=value` assignments mean it no longer starts with
`uv`. Pass extra arguments through instead: `./scripts/dev.sh test api -q -k session`.
