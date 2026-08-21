# Agent Design (Google ADK)

## Firestore session and memory services, extending ADK's shipped pair

Sessions, events, and memory live in Firestore alongside the domain data, under one
database and one authorization boundary.

**The pinned version is `google-adk==2.7.0`.** It ships a Python Firestore implementation
of both services:

```python
from google.adk.integrations.firestore.firestore_session_service import FirestoreSessionService
from google.adk.integrations.firestore.firestore_memory_service import FirestoreMemoryService
```

Neither is re-exported from `google.adk.sessions` / `google.adk.memory`, so they must be
imported from `google.adk.integrations.firestore.*` by full path.

This project **subclasses that pair** in `apps/api/src/coach/adk_firestore/` rather than
implementing `BaseSessionService` / `BaseMemoryService` from scratch. The shipped
implementation already does the parts that are tedious and easy to get wrong — async client,
state-delta scoping, optimistic concurrency — and what this project needs on top is small
and additive.

> **Important: the `google-adk` version is deliberately pinned. Do not bump it unless
> absolutely necessary.** Subclassing couples this project to the shipped classes'
> *internals*, not just to the abstract base classes, so the pin matters more here than it
> would for a from-scratch implementation — see
> [Bumping the ADK version](#bumping-the-adk-version) below.

Note when consulting documentation: ADK ships SDKs for several languages, and
`adk.dev/integrations/firestore-session-service/` documents the **Java** SDK
(`com.google.adk.sessions.FirestoreSessionService`), whose shape differs from the Python
class of the same name. The installed Python source is the reference for both.

### What the shipped `FirestoreSessionService` already provides

Verified against the installed 2.7.0 source:

- **Async throughout.** Constructed as `FirestoreSessionService(client=AsyncClient(...),
  root_collection=...)`; every path uses `firestore.AsyncClient`, so it never blocks the
  event loop. (ADK Java's implementation blocks deliberately; the Python one does not.)
- **State-delta scoping.** `append_event` runs a transaction that routes
  `event.actions.state_delta` by prefix — session state onto the session doc, `user:` keys
  onto `user_states/{app}/users/{uid}`, `app:` keys onto `app_states/{app}`, `temp:`
  applied in memory then trimmed before persistence.
- **Optimistic concurrency.** The session doc carries a `revision` integer. `append_event`
  compares it against the loaded session's `_storage_update_marker` and raises
  `StaleSessionError` on mismatch, so a session mutated by another writer cannot be
  appended to blindly. `Runner` catches `StaleSessionError` and reloads.
- **In-process serialization.** Per-`(app, user, session)` `asyncio.Lock` around
  `append_event`, so concurrent appends within one instance queue rather than collide.
- **Partial events are not persisted.** `append_event` returns early on `event.partial`,
  matching ADK's own semantics and keeping write costs bounded.
- **Bounded history.** `get_session` honours `GetSessionConfig.num_recent_events` (via
  `limit_to_last`) and `after_timestamp`. `num_recent_events == 0` skips the events query
  entirely.

### What the subclass adds

`CoachSessionService(FirestoreSessionService)` adds exactly four things:

| Addition | Why | How |
| --- | --- | --- |
| `seq` on each event doc | `GET /api/sessions/{sid}/events?after_seq=` pages the transcript deterministically; `timestamp` ordering alone is not a stable cursor | Override `append_event`; the shipped transaction already computes `new_revision`, which increments once per appended event and is therefore already a gap-free per-session sequence. Persist it as `seq`. |
| `projectId` / `taskId` linkage on the session doc | `GET /api/sessions/{sid}` returns linkage; the board resolves a session to its task | Set on `create_session`, merged into the session document |
| `get_user_state()` | The shipped class does **not** override it, so it inherits `BaseSessionService`'s `NotImplementedError`. The prompt builder reads user-scoped state at session start without loading a session | Read `user_states/{app}/users/{uid}` directly and strip the `user:` prefix |
| `flush()` | Inherited no-op is correct, but is overridden explicitly so the contract suite covers it | No-op, asserted |

Overriding `append_event` means reimplementing the shipped transaction rather than hooking
into it — there is no extension point inside it. The subclass keeps the shipped body's
structure (including the `revision` check and `StaleSessionError`) and adds the `seq` and
linkage writes. **That copied transaction body is the single most bump-sensitive thing in
this project**; it is the first item on the bump checklist below.

Contract tests run the same suite against `InMemorySessionService` and ours to catch
behavioural drift ([08-testing.md](08-testing.md)).

### `CoachMemoryService(FirestoreMemoryService)`

The shipped `FirestoreMemoryService` already implements v1 exactly as this project wants it:
keyword extraction with a stop-word list, storage with a `keywords[]` array, and
`search_memory` as an `array_contains` fan-out over query terms. `add_session_to_memory` and
`search_memory` are used as-is.

The subclass adds only per-user collection placement (`users/{uid}/memories/{memoryId}`)
and the `sourceSessionId` / `projectId` fields the UI attributes memories by.

v2 upgrade path (M7+): add an `embedding` vector field and use Firestore's `find_nearest`
KNN with `text-embedding-004`. The `BaseMemoryService` interface does not change, so this
is a drop-in swap behind a config flag.

### Two different `seq` spaces — do not conflate them

The word `seq` appears in two unrelated places. Conflating them leads to the conclusion that
turn resume depends on the session service's document layout. It does not:

| Name | Scope | Lives in | Used for |
| --- | --- | --- | --- |
| Event `seq` | Per **session**, one per finalized ADK event | `…/events/{eventId}.seq` | Transcript pagination (`?after_seq=`) |
| Delta `seq` | Per **turn**, one per streamed chunk | `turns/{turnId}.checkpoints` and WS frames | Disconnect/resume replay ([04-api-contract.md](04-api-contract.md)) |

Turn resume reads `turns/*` and never touches the session service. This is why adopting the
shipped session service costs nothing on the resume path.

### Bumping the ADK version

The pin exists because this project subclasses two concrete ADK classes, so the coupling is
to their **internals**, not merely to the abstract base classes. Treat a bump as a scheduled
task with its own verification pass, not as routine dependency maintenance.

What to re-verify against the newly installed source, and re-test:

| Surface | Why it matters here | Lands in |
| --- | --- | --- |
| **`FirestoreSessionService.append_event` transaction body** | We reimplement it to add `seq`. Any upstream change to the `revision` check, the state-delta split, or the event document shape must be mirrored by hand — this is the highest-risk item | `adk_firestore/session_service.py` |
| `revision` semantics and `Session._storage_update_marker` | We derive event `seq` from `revision`; if it stops incrementing once per event, `seq` silently develops gaps or duplicates | same |
| `StaleSessionError` — when it is raised, and that `Runner` still catches it | Our override must keep raising it or concurrent appends corrupt state | same |
| Collection layout constants (`DEFAULT_ROOT_COLLECTION`, `app_states`, `user_states`) and `_get_sessions_ref` | We inherit the layout and read `user_states` directly in `get_user_state` | same, and [02-data-model.md](02-data-model.md) |
| `create_session` / `get_session` / `list_sessions` / `delete_session` signatures | Inherited or lightly extended | same |
| `GetSessionConfig` (`num_recent_events`, `after_timestamp`), `ListSessionsResponse` | Drives the bounded-history query that keeps long sessions cheap | same |
| `Event` fields, and partial vs finalized semantics | We persist only finalized events; a change alters write volume | same |
| `get_user_state`, `flush` | We override; upstream may start providing them | same |
| `FirestoreMemoryService` keyword extraction, stop-word list, `search_memory` fan-out | Used as-is; a change moves retrieval quality | `adk_firestore/memory_service.py` |
| `Runner` streaming event shape | Feeds delta `seq` assignment, checkpointing, and the resume path | `ws/`, `services/` |
| `GcsArtifactService(bucket_name, **kwargs)` construction, `_get_blob_name`, `types.Part` file references | Upload and multimodal path. `artifact_part_uri` reads the blob layout out of the private method deliberately, so a rename fails a test rather than a deploy | `integrations/` |
| `InvocationContext`'s `artifact_service` and `session_service` fields, and where `Runner` builds the context | Pydantic validates both with `isinstance`. That is why the artifact service is deferred with a *provider* and not a proxy — a proxy passed every local test and failed every deployed turn | `integrations/artifacts.py`, `agents/runner.py` |
| Tool and callback signatures | The tool catalogue below | `agents/` |
| **`check_require_confirmation`'s calling convention** | A *callable* `require_confirmation` — `complete_task_item`'s, which reads the project's opt-out — is invoked with the **tool's own arguments**, not with a context: `_prepare_invocation_args` filters them against `FunctionTool.func`'s signature and calls `callable(**args)`. A signature that does not absorb them raises `TypeError` inside the flow, surfacing as `DynamicNodeFailError` and a failed turn rather than as anything naming the gate | `agents/tools.py` |
| **`FunctionTool(require_confirmation=…)` and the `adk_request_confirmation` handshake** | The gates on `discard_task` and `complete_task_item` are ADK's, not ours. The constant is restated in `services/turns.py` and again in `apps/web/src/lib/transcript.ts`, which cannot import it, and the *ordering* of the events ADK emits is what the UI reads — the request is second-from-last, not last. `ToolConfirmation` is `@experimental`. From M4 a bump that broke this would silently take the click out of task completion, not merely out of discarding | `agents/tools.py`, `services/turns.py`, `lib/transcript.ts` |
| `Context` (`ToolContext` == `CallbackContext` == `Context` in 2.7) — `user_id`, `state`, and `before_agent_callback`'s keyword | Every per-invocation fact a tool has arrives through these two fields; `state` write-through to `session.state` is what makes `{temp:…}` templating see them | `agents/context.py`, `agents/prompt.py` |
| `inject_session_state` — the `{key}` / `{key?}` grammar and its `KeyError` | A placeholder with no writer fails while assembling the request, inside the detached generation task, on the first real turn of a deployed revision | `agents/project_coach.py`, `agents/task_teacher.py`, `tests/test_agent_prompt.py` |
| `_convert_tool_union_to_tools` / `canonical_tools` built-in-tool wrapping | Decides whether `google_search` may sit beside function tools. If a bump lifts the Gemini restriction, the explicit `search_agent` hop becomes deletable ([M1 spike result](#m1-spike-result-resolved-against-the-installed-270-source)) | `agents/` |

**Order of work:** install the new version, run the contract suite
([08-testing.md](08-testing.md)) — identical tests against `InMemorySessionService` and ours,
so a moved semantic fails on the in-memory side first and names itself — then **diff the
shipped `firestore_session_service.py` against the copy our override was derived from**,
since that is the one surface the contract suite cannot see, then run the streaming and
resume suites, which are where `Runner` changes surface.

Two exits if the subclass ever stops earning its place: drop to the shipped class unmodified
and page the transcript by `timestamp` instead of `seq`; or, if Firestore itself stops
fitting, `DatabaseSessionService` against Cloud SQL Postgres — which costs a managed database
and a connection pool on Cloud Run, and gives up the single-datastore property. Neither is
right for v1, but both are real.

### Artifacts

`GcsArtifactService` from ADK, pointed at `gs://{project}-coach-artifacts`. User uploads
(images, PDFs) land there and are referenced as `types.Part` file parts. No custom work.

One wiring constraint, learned in production: its constructor resolves credentials, so it
cannot be built while the container is assembled — and it is handed to ADK, which
`isinstance`-checks it, so it cannot be a proxy either. The container therefore holds a
provider, `integrations/artifacts.artifact_service_provider`, and `UploadService` and
`RunnerFactory` resolve it when a request first needs the bucket.

## Agent graph

**Since M6** ([09-roadmap.md](09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher))
the single interactive coach is two agents, chosen by `services/turns.py`'s
`_resolve_agent` from the turn's own session linkage — `project_coach` when `taskId` is
`null`, `task_teacher` when it names a task. Splitting the routing off the agent rather
than leaving one instruction to say which conversation it is having is the fix for a
reported bug: asked to add optional topics to a study plan from inside a task's own
conversation, the single coach reached for `add_task` — onto the *board* — where the
learner meant `add_subtask`, inside the task in front of them. `task_teacher` has no
`add_task` tool at all, so it cannot make that mistake irrespective of what the prompt
says.

```
                       ┌────────────────────────┐
   interactive,       │      project_coach      │  LlmAgent, thinking_level=high
   taskId: null ─────▶│   (Socratic guide,      │
                       │  the board as a whole)  │
                       └───────┬────────────────┘
                               │ tools
        ┌──────────────────────┼────────────────────────────┐
        ▼                      ▼                            ▼
  board-level tools      memory tools               AgentTool(research_agent)
  (add_task, discard_     load_memory,
  task, reorder_task, …)  update_learner_profile

                       ┌────────────────────────┐
   interactive,        │      task_teacher       │  LlmAgent, thinking_level=high
   taskId: set ───────▶│  (Socratic guide,      │
                       │   one task's checklist) │
                       └───────┬────────────────┘
                               │ tools
        ┌──────────────────────┼────────────────────────────┐
        ▼                      ▼                            ▼
  checklist tools         memory tools               AgentTool(research_agent)
  (add_subtask,           load_memory,
  add_task_items, …)      update_learner_profile

                       ┌────────────────────────┐
   scheduled ─────────▶│  autonomous_workflow   │  SequentialAgent
                       └───────┬────────────────┘
                               │
        ┌──────────┬───────────┼───────────┬──────────────┐
        ▼          ▼           ▼           ▼              ▼
  select_next_  research_agent  post_    propose_tasks  reprioritize
  task          (LlmAgent)      report   (LlmAgent)     (code)
  (code, not                    (code)
   an LLM)

                       ┌────────────────────────┐
                       │     research_agent     │  LlmAgent, thinking_level=high
                       │  tools: AgentTool(search_agent),
                       │         fetch_url,
                       │         youtube_find_by_duration,
                       │         post_research_report
                       └───────┬────────────────┘
                               │
                       ┌───────▼────────────────┐
                       │      search_agent      │  LlmAgent with ONLY the
                       │  tools: google_search   │  built-in google_search tool
                       └────────────────────────┘
```

### Why `search_agent` is separate

ADK/Gemini restricts mixing built-in tools (like `google_search`) with custom function
tools in a single agent. The standard workaround is to isolate the built-in tool in its
own `LlmAgent` and expose it to the parent via `AgentTool`.

#### M1 spike result (resolved against the installed 2.7.0 source)

The M1 spike ([09-roadmap.md](09-roadmap.md#m1--domain-core-no-agent-15-weeks)) asked
whether the built-in `google_search` tool can be combined with custom function tools in a
single agent. Answer, read out of
`.venv/lib/python3.12/site-packages/google/adk/agents/llm_agent.py`:

- `LlmAgent.canonical_tools` (~line 757) computes `multiple_tools = len(self.tools) > 1`
  and passes it to `_convert_tool_union_to_tools` (~line 139).
- That function wraps a `GoogleSearchTool` into `GoogleSearchAgentTool(create_google_search_agent(model))`
  — i.e. builds the search-agent hop for you — but **only** when `multiple_tools` is true
  *and* `GoogleSearchTool.bypass_multi_tools_limit` is true. That flag defaults to `False`
  (`tools/google_search_tool.py`, line 42), so nothing happens implicitly.

So the **Gemini-level restriction still holds**. ADK has not lifted it; it has shipped, as
an opt-in workaround, exactly the isolate-in-a-sub-agent hop this document already
describes by hand. The two options are therefore behaviourally the same shape:

| Option | What it means | Cost |
| --- | --- | --- |
| **A — explicit `search_agent` (chosen)** | Keep the `search_agent` `LlmAgent` and expose it to `research_agent` as an `AgentTool`, as drawn in the graph above | One hop, authored by us, visible in the agent graph and in traces |
| B — `GoogleSearchTool(bypass_multi_tools_limit=True)` | Put `google_search` directly in `research_agent`'s tool list and let ADK generate the wrapper | Same hop, generated; depends on an internal wrapping rule and a non-default flag |

**Decision: A.** The hop is not avoidable either way, so the only thing on the table is who
writes it. Option B moves a load-bearing behaviour inside ADK internals that the pin
already makes us cautious about, and buys nothing at runtime. Option A keeps the sub-agent's
model, instruction, and `thinking_level` under our control, which matters because
`create_google_search_agent` picks those for us.

Either option is compatible with the rest of the design; switching later is a local change
in `agents/`. Add a row to the bump checklist below for whichever is in force.

### `project_coach` and `task_teacher`

**Since M6** these are two agents rather than one — see
[09-roadmap.md](09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher)
for why the split happened and what it fixes. `services/turns.py`'s `_resolve_agent`
picks between them from the turn's session linkage, so which agent answers is a fact
about the session (`taskId: null` or not), never something either instruction has to
argue for in prose.

#### `project_coach`

Purpose: the conversation about the project as a whole — the intake conversation that
elicits a goal and proposes a first task list, and every later conversation about the
board. Its session's `taskId` is always `null`.

Behaviour encoded in the instruction:
- Ask Socratic questions to elicit the project goal and constraints before proposing
  tasks; do not produce a task list from a one-line prompt.
- Respect `EffectivePrefs.defaultTaskMinutes` when sizing tasks; if a proposed task
  exceeds it by >50 %, break it into subtasks with `add_subtask` — one at a time, as the
  learner agrees to each, rather than a whole breakdown proposed at once (golden flow #2).
- Discarding a task, and marking one complete, are gated or refused the same way
  regardless of which agent proposes them — see the tool catalogue below.

**It has no item-level tool at all.** A checklist belongs to one task, and this agent's
session is never linked to one, so `add_task_items`, `update_task_item`,
`reorder_task_item`, `move_task_items`, `delete_task_item`, and `complete_task_item` are
absent from its catalogue — not merely discouraged in the instruction.

#### `task_teacher`

Purpose: the conversation the learner has while working on one task. Its session is
always linked to that task.

Behaviour encoded in the instruction:
- **Treat the learner's task-duration preference as guidance for the checklist, too.** It
  is how long the learner wants one sitting to be, so a checklist that outgrows it is
  usually two pieces of work rather than one long one. `add_task_items` and
  `delete_task_item` return the running total against it once it is exceeded — a *fact*,
  not a refusal. There is deliberately no guard: a 50-minute plan on a 45-minute task is a
  rounding difference, and the coach with the learner in front of it is better placed to
  judge than a rule. Task-level overrides arrive with the learner model at M7.
- When the user uploads work, analyse it against the task's success criteria and respond
  in the learner's `guidanceStyle`.
- Never claim material was read that was not fetched; cite the tool result.
- **Work the task's items in order, and treat `guided` as the instruction it is.** On a
  guided item, teach it — the exercise, the walkthrough, the questions. On an unguided one,
  hand over what the item says to go and do and then stop, because the work is happening
  somewhere the coach cannot see; ask afterwards how it went rather than narrating a page or
  a video it never opened. Propose `complete_task_item` when an item looks done; the
  learner's confirmation is what finishes it.
- **Everything the learner describes as part of what they are doing now is scoped inside
  this task.** `add_subtask` for a piece worth tracking on its own, `add_task_items` for a
  step on the checklist. This is the fix stated structurally: **there is no `add_task`
  tool here**, so nothing the learner says in this conversation can land beside the task
  on the board — the mistake that prompted the split in the first place.

Both agents share one `before_agent_callback` that injects project goal, effective prefs,
current task + its subtasks, last N task outcomes, and the `learnerProfile` summary.
Injected as state, not as a giant literal prompt, so ADK's `{state_key}` templating keeps
the prompt cache-friendly.

### `research_agent`

Input: `{ taskId, budgetMinutes, projectGoal, prefs, learnerProfile }`.
Output: exactly one `post_research_report` call.

Workflow it is instructed to follow:
1. `search_agent("…")` for authoritative material; note grounding citations.
2. `fetch_url` on the 2–4 most promising results to confirm they actually cover the task
   (title-based selection is how bad reading lists happen).
3. If `prefs.allowVideos`, `youtube_find_by_duration(query, max_minutes=remaining_budget)`.
4. Optionally author an exercise or `code_scaffold` item itself.
5. Call `post_research_report` with `required[]` sized to fit `budgetMinutes` and
   everything else in `optional[]`.

**`required[]` is a plan for the task, not a bibliography, because it becomes the task's
checklist** ([02-data-model.md](02-data-model.md#task-items)). Two things follow that the
instruction has to say out loud. Every required item needs a `why` written in the second
person and in the learner's own terms — it renders as the item's `shortDescription`, so a
`why` that reads "provides necessary background" produces a checklist nobody can act on.
And the ordering of `required[]` is the order the work happens in: reading before the
exercise that uses it, setup before the thing being set up. The tool preserves the array's
order rather than sorting by kind or by minutes.

`research_agent` has **no board-mutating tools**. It reads the task and answers with exactly
one `post_research_report` call; adding, splitting, and reordering tasks belong to
`propose_tasks`, a separate step with its own bounded budget. That separation is what
[10-risks.md](10-risks.md#r7--prompt-injection-via-fetched-pages-and-uploads) means by
"tools that mutate state are unavailable to `research_agent` except `post_research_report`",
and it matters most here: this is the agent with fetched web pages in its context.

### `autonomous_workflow` (SequentialAgent)

Steps are individually checkpointed by the run ledger, so each is separately resumable:

| # | Step | Kind | Notes |
| --- | --- | --- | --- |
| 1 | `select_next_task` | code | Deterministic: a task the learner **queued** (`researchStatus == "pending"`) if there is one, oldest `researchRequestedAt` first; otherwise the lowest `order` among `draft`/`not_started`/`in_progress`, skipping `completed`, `discarded`, `postponed`, and unexpired `postponed_until`. No LLM. A run that took the project *because* something was requested has to research that thing ([05-autonomous-runs.md](05-autonomous-runs.md#candidate-selection-and-guards)). |
| 2 | `research` | `research_agent` | Skipped if `task.needsResearch == false`. |
| 3 | `post_report` | code | Writes `research_reports/*`, promotes `required[]` into the task's `items[]`, appends a `research_report_ref` event to the task's session, sets `researchStatus = done` — and promotes the task out of `draft` if the items are its first plan. |
| 4 | `propose_tasks` | LlmAgent | May emit `add_task` / `add_subtask` calls if research revealed missing prerequisites. Bounded: ≤ 5 new tasks per run. |
| 5 | `reprioritize` | code | Applies the agent's requested `set_next_up` / ordering via fractional index writes. |

Step 1 and 5 are deliberately not LLM steps — ordering and selection are rules, and
making them rules removes a whole class of nondeterminism from background behaviour.

## Tool catalogue

All tools are typed `FunctionTool`s with Pydantic argument models, wrapping `services/`.
Every tool returns a compact structured result (not prose) so the model reasons over facts.

The domain tools landed at M3 in `agents/tools.py`. Two things about them are decisions
rather than mechanics, and both are argued in
[09-roadmap.md](09-roadmap.md#status-after-m3): a guard **answers** rather than raises, so
a refused call is a fact the model can act on instead of the end of the turn; and a tool
reads whose board it is acting on from the invocation, never from an argument — the uid
from the session and the project from `temp:` state written by the prompt callback — so a
tool cannot be pointed at someone else's project by an argument the model chose.

### Domain tools

`DomainTools.as_project_tools()` and `DomainTools.as_task_tools()` build the two
interactive catalogues; `as_autonomous_tools()` is `propose_tasks`'s subset of the same
methods (`agents/tools.py`). The **Agent** column below is which of the two interactive
catalogues carries the tool — `discard_task` and `ask_learner` are on both, everything
else is on exactly one, and the empty intersection with the other's exclusive tools is
the point: `project_coach` has no item-level tool, and `task_teacher` has no `add_task`
([09-roadmap.md](09-roadmap.md#m6--splitting-the-coach-into-a-project-coach-and-a-task-teacher)).

| Tool | Agent | Signature (abridged) | Guard |
| --- | --- | --- | --- |
| `list_tasks` | both | `(project_id, include_completed=False)` | owner |
| `add_task` | `project_coach` | `(project_id, title, description, estimated_minutes, needs_research, after_task_id=None)` | ≤ 5/run; minutes ≤ 3× default |
| `update_task` | `project_coach` | `(task_id, title?, description?, estimated_minutes?)` | owner |
| `set_task_state` | `project_coach` | `(task_id, state, postponed_until?)` | state machine validated server-side; **`completed`, `discarded`, and `draft` are refused** — the first is the learner's click ([10-risks.md](10-risks.md#open-questions) Q1), the second would bypass `discard_task`'s gate, and the third is a state a task *leaves*, never one it is put into |
| `set_next_up` | `project_coach` | `(task_id)` | moves the task to the front of the board's top-level order. It no longer writes a state: `project.nextUpTaskId` became derived when `in_progress` replaced `current` ([02-data-model.md](02-data-model.md#task-state-machine)), so pinning something is a reorder, and "next up" and "started" stopped being the same claim |
| `reorder_task` | `project_coach` | `(task_id, after_task_id \| before_task_id)` | fractional index |
| `discard_task` | both | `(task_id, reason)` | **requires user confirmation** in interactive mode; forbidden in autonomous mode |
| `add_subtask` | both | `(task_id, title, description, estimated_minutes, needs_research)` | one level deep; ≤ default minutes, a stricter bound than `add_task`'s because a piece that still does not fit has not done the thing breaking up is for. **The first subtask inherits the parent's checklist**, because a composite task cannot hold one ([02-data-model.md](02-data-model.md#task-items)) and dropping the items would lose work the learner may have half-finished |
| `update_task_item` | `task_teacher` | `(item_id, short_description?, details?, guided?, subtask_id?)` | leaf tasks only |
| `reorder_task_item` | `task_teacher` | `(item_id, after_item_id \| before_item_id, subtask_id?)` | array positions, rewritten whole |
| `move_task_items` | `task_teacher` | `(item_ids[], to_subtask_id, from_subtask_id?)` | moves several steps between a task and its subtasks in one transaction, keeping their ids and their ticks. **Not** confirmation-gated — see below |
| `delete_task_item` | `task_teacher` | `(item_id, reason, subtask_id?)` | **requires user confirmation** — destructive, *and* removing the last outstanding step completes the task, which would otherwise be a route around `complete_task_item`'s gate |
| `ask_learner` | both | `(question, options[], allow_multiple, allow_none, note_prompt)` | 2–6 options. Asks a question rather than for approval, so it posts its own confirmation carrying the question as the payload; the answer comes back as a selection. The selection is filtered against the options that were offered |
| `complete_task_item` | `task_teacher` | `(item_id, note)` | **requires user confirmation**, on the same ADK handshake as `discard_task` — an item completing can complete the whole task ([02-data-model.md](02-data-model.md#task-items)), so the last word before that stays the learner's. Scoped to the session's own task; the tool takes no task id |
| `add_task_items` | `task_teacher` | `(items[], subtask_id?)` | appends items. Leaf tasks only; refused on a task with subtasks. Used when the conversation turns up work the report did not anticipate |
| `update_project_prefs` | `project_coach` | `(default_task_minutes?, research_depth?, allow_videos?)` | one named argument per writable key — spelling them out *is* the whitelist, where a patch object would let the model invent fields |
| `post_research_report` | — (`research_agent`) | `(task_id, summary, required[], optional[])` | validates `Σ required.minutes ≤ budget`, assigns `itemId`s, writes the report, and promotes `required[]` into `tasks/{id}.items[]` in one transaction |

### Asking the learner something

`ask_learner` is the odd one in the catalogue: it uses ADK's confirmation handshake for
something that is not a confirmation. `ToolConfirmation` carries a free-form `payload`
beside `confirmed`, and a tool that calls `tool_context.request_confirmation(hint, payload)`
**from inside its own body** — rather than being declared `require_confirmation=True` —
chooses what goes in it. So the question and its options ride out in the payload, the
client renders controls from them, and the selection rides back in the answer's payload.

The tool is therefore invoked **twice for one call**: once to ask, and once — the same call
id, re-executed by `_RequestConfirmationLlmRequestProcessor` — to read the answer. That is
why its body reads "have I been answered yet?" rather than being two tools with a state
machine between them.

Three consequences worth stating, because each is a thing to get wrong:

- **The static flag would not do.** `FunctionTool(require_confirmation=True)` posts ADK's
  own generic hint and no payload, which is right for a yes/no gate and carries nothing for
  a question.
- **A declined question is a result, not a failure.** "None of these" is frequently the
  honest answer; the tool returns `answered: false` so the coach can act on it.
- **The answer is not authoritative.** It has been through the client, so the selection is
  filtered against the options the tool itself offered — a small surface, and free to close.

**The gate on `complete_task_item` is a project preference, and the only one that is.**
`confirmItemCompletion` defaults on, and `require_confirmation` is a *callable* rather than
`True` so the setting takes effect on the next completion rather than the next process
restart. It reads `temp:` state rather than the project document, because ADK evaluates it
while assembling the tool call and a Firestore read there would be one per gated call; the
prompt callback resolves the preference alongside every other.

`discard_task` and `delete_task_item` stay statically gated. The preference is about the
*friction* of confirming routine completions, and neither of those is routine or
recoverable — a learner who silenced one did not ask for the others.

The dialog offers the setting as a third button ("and stop asking in this project"),
because the moment the friction becomes obvious is the moment you are looking at it. That
flag rides in the confirmation's answer payload rather than being a second request, so one
click is one round trip and the preference cannot land without the completion it was
attached to.

**`complete_task_item` is the only tool that can finish a task, and it can only do so by
asking** — unless the learner has said otherwise for this project, which is itself a
click. The chain is deliberate and worth stating in one place: the tool call becomes an
`adk_request_confirmation`, the learner answers, the body writes `completed: true` on the
item, and `TaskService` evaluates invariant 6 in that same transaction — so a task
completing is always downstream of a click. `set_task_state` still refuses `completed`
outright, which keeps the direct route closed while the indirect one is gated.

### Which task an item tool acts on

Every item tool takes an optional `subtask_id`, defaulting to the task the conversation is
about. That argument is **bounded, not free**: the session's own task, or one of its
children, and anything else is refused.

The original design took no task id at all, on the reasoning that a task-scoped session is
the only place these tools are useful and an argument naming a task would be a way to point
them somewhere else. That was right about the risk and wrong about the scope. Breaking a
task down makes the session's task a *parent*, and a parent holds no checklist — so every
item tool stopped working the moment the coach did the thing it had just been asked to do,
answering "this task has subtasks, and its subtasks are its plan" to calls about steps that
were sitting on a subtask, unreachable. The bounded argument keeps the property the
original reasoning protected while making a broken-down task's actual plan editable.

`render_focus` renders each subtask **with its checklist** for the same reason: the ids
these tools take have to be visible somewhere, and a focus section listing subtasks by
title left the coach unable to see the plan it had just made.

### Moving steps rather than deleting and re-adding

`move_task_items` exists because adding the first subtask hands it the *whole* checklist —
a task's plan is its items or its subtasks and never both — so the steps belonging to the
second subtask start out on the first. Redistributing them had no instrument: delete and
re-add was the only route, and it loses the item's id (and with it any feedback recorded
against the recommendation it came from) and asks the learner to approve every removal.

**It is not gated, and `delete_task_item` is.** Both can complete a task by removing its
last outstanding step, which is the reason deletion is gated at all. The difference is
that deletion makes work *vanish*, where a move leaves it visibly on another task — so a
source that completes has made a true statement about where the work is. Gating it would
also turn redistributing a ten-step checklist into ten approvals, which is the cost that
sent the coach back to deleting in the first place.

### Memory tools

| Tool | Notes |
| --- | --- |
| `load_memory` | ADK's standard memory retrieval, backed by `FirestoreMemoryService`. |
| `update_learner_profile` | Typed patch on `users/{uid}.learnerProfile`. Rate-limited to 1 call/turn; every write audited with the triggering session id. |
| `remember` | Writes a single durable memory entry with text + tags. |

### Integration tools

| Tool | Notes |
| --- | --- |
| `google_search` | ADK built-in, inside `search_agent` only. Grounding citations captured into the report. |
| `fetch_url` | Server-side fetch with SSRF guards (no private IP ranges, no redirects to them), 2 MB cap, 10 s timeout, HTML→markdown, robots-respecting. |
| `youtube_find_by_duration` | `search.list` → `videos.list(part=contentDetails,statistics)` → parse ISO-8601 → filter `duration ≤ max_minutes` → rank by view/like ratio and recency. Returns ≤ 8 candidates with exact minutes. Quota-guarded and cached 24 h by query hash. |

## The learner model, concretely

Three layers, deliberately separated by durability:

| Layer | Where | Written by | Used for |
| --- | --- | --- | --- |
| Session state | `sessions/{id}.state` | ADK state deltas | Within-conversation context |
| User state | `user_states/{app}/users/{uid}` (ADK `user:` scope) | tools | Cross-session facts ADK needs at session start |
| Learner profile | `users/{uid}.learnerProfile` | `update_learner_profile` only | Prompt construction, Settings UI, adaptation |
| Episodic memory | `users/{uid}/memories/*` | `add_session_to_memory` on session close + `remember` | `load_memory` retrieval |

**Adaptation loop.** At session close (or after N turns), an `after_agent_callback`
summarizes the session into memory and, if warranted, proposes a profile patch. The
patch is applied only through the typed tool, is versioned, and is shown to the user in
Settings as "what the coach believes about how you learn," with per-field edit and reset.
This makes the "evolving user model" inspectable rather than a black box — which also
makes it debuggable when the coach starts behaving oddly.

## Safety rails on autonomy

- Autonomous mode runs with a **reduced tool set**: no `discard_task`, no
  `update_learner_profile`, no `update_project_prefs`. Background work may add, research,
  split, and reorder — it may not silently redefine the user's goals or delete their work.
- Every autonomous mutation records `origin: "agent"` and `runId`, and the UI badges
  agent-created tasks so the user can see what happened while they were away.
- A per-run cap on tool calls and tokens; exceeding it fails the step cleanly and is
  retried with a smaller scope rather than looping.
