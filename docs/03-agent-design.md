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
| **`Workflow`, its graph (`edges`), and `single_turn` mode's `include_contents` default** (since M9) | `research_workflow` is this project's only use of ADK's graph orchestrator. A change to whether a standalone `LlmAgent` node still defaults to `mode="single_turn"` with `include_contents="none"` is the difference between `topic_researcher` staying clean and it silently seeing the planner's turn or a sibling's — with no error, since nothing about extra context is invalid, just wrong (`isolation_scope` does **not** provide this isolation for a `_ParallelWorker` fan-out — see [the research pipeline](#the-research-pipeline-since-m9) for why) | `agents/research_workflow.py` |
| `node(..., parallel_worker=True, max_parallel_workers=…)` / `_ParallelWorker` | Gives `topic_researcher` its runtime-sized (3–5) fan-out. A signature change here is a construction-time failure, which is the easy case; a *semantic* change to how it orders or bounds concurrent branches is not, and is exactly what the throttle above is not allowed to assume away | `agents/research_workflow.py` |
| `before_model_callback` / `after_model_callback` signatures | The M9 throttle acquires and releases its per-run semaphore here. A signature change that this project's callback does not absorb fails the same way `check_require_confirmation`'s does — inside the flow, as a failed turn, not as anything naming the throttle | `agents/research_workflow.py` |
| Whether `Workflow` may be handed to `Runner.run_async` as a turn's root, same as any `BaseAgent` | `TurnService.start` picks `research_workflow` for the (single) `research` step the same way it already picks `research_agent`/`propose_tasks`/etc. for the others — confirmed directly in `runners.py`'s `isinstance(self.agent, BaseNode) and not isinstance(self.agent, BaseAgent)` branch, see [`Workflow` as a turn root, and retrying a crash mid-fan-out](#workflow-as-a-turn-root-and-retrying-a-crash-mid-fan-out) | `services/turns.py` |

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
   taskId: null ─────▶│   (Socratic guide,      │──── "Research this" button ────┐
                       │  the board as a whole)  │     (a REST trigger, not a tool)│
                       └───────┬────────────────┘                                │
                               │ tools                                           │
                  ┌────────────┴────────────┐                                    │
                  ▼                         ▼                                    │
            board-level tools         memory tools                               │
            (add_task, discard_       load_memory,                               │
            task, reorder_task, …)    update_learner_profile                     │
                                                                                  │
                       ┌────────────────────────┐                                │
   interactive,        │      task_teacher       │  LlmAgent, thinking_level=high │
   taskId: set ───────▶│  (Socratic guide,      │──── "Research this" button ────┤
                       │   one task's checklist) │     (a REST trigger, not a tool)│
                       └───────┬────────────────┘                                │
                               │ tools                                           │
                  ┌────────────┴────────────┐                                    │
                  ▼                         ▼                                    │
            checklist tools           memory tools                               │
            (add_subtask,             load_memory,                               │
            add_task_items, …)        update_learner_profile                     │
                                                                                  │
                       ┌────────────────────────┐                                │
   scheduled ─────────▶│  autonomous_workflow   │  SequentialAgent               │
                       └───────┬────────────────┘                                │
                               │                                                 │
        ┌──────────┬───────────┼───────────┬──────────────┐                     │
        ▼          ▼           ▼           ▼              ▼                     │
  select_next_  research_    post_    propose_tasks  reprioritize                │
  task          workflow     report   (LlmAgent)     (code)                     │
  (code, not    (since M9 —  (code)                                             │
   an LLM)      see below)          ▲                                          │
                       └────────────────────────────────────────────────────────┘
                       in a fresh session created for the run, never the caller's own (M8)

                       ┌──────────────────────────────────────────────────────┐
                       │       research_workflow  (ADK Workflow, since M9)      │
                       │  replaces the single research_agent turn               │
                       │                                                        │
                       │  ┌─────────────────┐                                   │
                       │  │ research_planner │  LlmAgent — topic + details,     │
                       │  │                  │  NO duration budget              │
                       │  └────────┬─────────┘                                  │
                       │           │ 3–5 sub-topics                             │
                       │           ▼                                            │
                       │  ┌─────────────────────────────┐                       │
                       │  │  topic_researcher × 3–5       │  LlmAgent,           │
                       │  │  parallel_worker fan-out,      │  one per sub-topic, │
                       │  │  default single_turn mode       │  NO duration budget│
                       │  │  (include_contents="none":     │                    │
                       │  │  no visibility into the        │                    │
                       │  │  planner or siblings)          │                    │
                       │  │  tools: AgentTool(search_agent),                    │
                       │  │  fetch_url, youtube_find_by_duration                │
                       │  │  throttled: ≤1 LLM call in flight per run           │
                       │  └────────────────┬────────────────┘                  │
                       │                    │ N reports                        │
                       │                    ▼                                  │
                       │  ┌───────────────────────────────┐                    │
                       │  │        reviewer_writer          │  LlmAgent, reads  │
                       │  │                                  │  the planner's   │
                       │  │  budget + prefs + research job   │  turn via        │
                       │  │  (the duration neither upstream  │  include_contents│
                       │  │  agent saw)                      │  ="default" (same│
                       │  │  tools: post_research_report      │  conversation)   │
                       │  └───────────────────────────────┘                    │
                       └──────────────────────────────────────────────────────┘

                       ┌────────────────────────┐
                       │      search_agent      │  LlmAgent with ONLY the
                       │  tools: google_search   │  built-in google_search tool
                       └────────────────────────┘
```

**No node of the research pipeline is a tool either `project_coach` or `task_teacher` can
call** — neither agent lists one in the tool catalogue below, and that stays true whether
the pipeline behind the button is the single `research_agent` turn or the `research_workflow`
graph that replaced it at M9. The relationship is real but indirect: a button in either agent's screen calls
`POST /api/sessions/{sid}/research` ([04-api-contract.md](04-api-contract.md#post-apisessionssidresearch)),
a plain REST endpoint outside the model's own turn, which starts `research_workflow` in a
session of its own. The learner's conversation and the research run are two separate
generations that happen to share a trigger, not one agent handing off to another
mid-turn — which is also why nothing in the research pipeline ever appears in either coach's
context and neither coach can be asked to "just call the research tool".

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
| **A — explicit `search_agent` (chosen)** | Keep the `search_agent` `LlmAgent` and expose it to the research pipeline's `topic_researcher` node (`research_agent`'s successor since M9) as an `AgentTool`, as drawn in the graph above | One hop, authored by us, visible in the agent graph and in traces |
| B — `GoogleSearchTool(bypass_multi_tools_limit=True)` | Put `google_search` directly in `topic_researcher`'s tool list and let ADK generate the wrapper | Same hop, generated; depends on an internal wrapping rule and a non-default flag |

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

Both agents share one `before_agent_callback` that injects effective prefs, current task +
its subtasks, last N task outcomes, and the `learnerProfile` summary. Injected as state,
not as a giant literal prompt, so ADK's `{state_key}` templating keeps the prompt
cache-friendly. **Neither instruction carries the project's own title/goal text** —
`PROJECT_KEY`/`render_project` existed through M9 and were removed once every agent's own
scope turned out to already come from board/task/item state (`{BOARD_KEY}`, `{FOCUS_KEY}`)
rather than from the project document directly; `project_coach` (the project-as-a-whole
conversation) still elicits and reasons about the goal through the ordinary conversation
history it already has, not through a re-rendered summary of it.

### The research pipeline (since M9)

**M9 splits the single `research_agent` turn into three LlmAgent nodes run by one
`research_workflow`.** The reported problem was breadth-versus-depth: one agent doing its
own planning, its own multi-source search, and its own final write-up spends its whole
context budget doing all three passably rather than any one well, and the fix isn't a
longer instruction — it's separating the passes into agents that each see less and can
therefore do their one job with a clean, un-crowded context. The M4-era section below is
carried forward wherever the decision it records is still true (the dedicated session, the
taskless capability, the read-only upload access); what changed is everything about how the
model actually does the research.

| Node | Kind | Input | Output |
| --- | --- | --- | --- |
| `research_planner` | LlmAgent | `{ topic, prefs }` — **no `budgetMinutes`** | 3–5 sub-topics |
| `topic_researcher` (× one per sub-topic) | LlmAgent, `parallel_worker` fan-out | `{ subTopic }` — **no `budgetMinutes`** | an ordered list of material references, each with its own estimated duration |
| `reviewer_writer` | LlmAgent | every `topic_researcher` output, the planner's own context, `budgetMinutes`, `prefs` | exactly one `post_research_report` call |

`topic` is `{ taskId, prefs, learnerProfile }` minus `budgetMinutes` for a task-scoped run,
or the taskless `reason` for one kicked off from the project coach's own conversation — see
below. The duration budget is withheld from both the planner and the topic researchers on
purpose: sizing the result into something that fits a sitting is a property of the *whole*
set of research, decided once the set is complete, not a constraint each sub-topic should
be narrowing itself against independently. Neither node (nor `reviewer_writer`) is handed
the project's own title/goal text either — same reasoning as `project_coach`/`task_teacher`
above: `topic`/`reason` already carries what this run is about.

**Session mechanics are unchanged from M8: one dedicated session per run, created fresh and
never reused** ([02-data-model.md](02-data-model.md#sessions--events-adk-owned-layout),
[09-roadmap.md](09-roadmap.md#m8--research-sessions-ui-rework-and-usage-quotas)).
`ResearchService` and `RunExecutor` still create it with
`CoachSessionService.create_session(..., kind="research", task_id=…, run_id=run.id)`,
still start **one turn** against that session id, and still record it on the run
(`autonomous_runs/{id}.sessionId`) and the report (`research_reports/{id}.sessionId`). What
M9 changes is what runs *inside* that one turn: not one agent, but the whole
`research_workflow` graph, run start to finish by a single `Runner.run_async` call — a
`Workflow` has no built-in "run this node and pause" primitive; once triggered, its
scheduling loop keeps draining ready nodes until none are left
([09-roadmap.md](09-roadmap.md#m9--reworking-the-autonomous-research-workflow) has the
ledger-shape consequence of that). **"Launched with a clean session" for `topic_researcher`
does not mean a second Firestore session document per sub-topic, and it does not mean ADK's
`isolation_scope` either** — nothing about the session collection changes, and
`isolation_scope` is computed by `Workflow._compute_isolation_scope_for_node` only for a
node the graph schedules *statically* (`Workflow._start_node_task`);
`_ParallelWorker._run_impl` dispatches each fan-out item *dynamically*, through
`ctx.run_node(self._node, node_input=item, use_sub_branch=True)`, which passes no
`override_isolation_scope` and therefore leaves every `topic_researcher` branch with
whatever scope the `_ParallelWorker` node itself had — `None`, since `_ParallelWorker` has
no `mode` field for `_compute_isolation_scope_for_node` to read in the first place.
`mode="task"` would not have isolated the fan-out from anything anyway; it would also have
pulled in `FinishTaskTool`/multi-turn "chat until finish_task" semantics — built for a chat
coordinator delegating to a task agent — that this pipeline has no use for.

What actually isolates `topic_researcher` is simpler and needs no `isolation_scope` at all.
`workflow/utils/_workflow_graph_utils.build_node` defaults a *standalone* `LlmAgent` node
(one with no `parent_agent`) to `mode="single_turn"`, and
`workflow/_llm_agent_wrapper.run_llm_agent_as_node` forces `include_contents="none"` for
`single_turn` unless the agent sets `include_contents` explicitly — meaning the LLM request
never includes prior session history at all, only the node's own `node_input` (its assigned
sub-topic). That is the whole of the isolation: not from the planner, not from siblings,
because nothing before this node's own input is ever read. `research_planner` and
`topic_researcher` both stay at this default (`mode` unset, `include_contents` unset).
`reviewer_writer` is the one exception: it sets `include_contents="default"` explicitly,
which is what lets it read `research_planner`'s own turn as ordinary prior history — the
mechanism behind "the reviewer-writer agent should have the context of the planner … by
working in the same session." `use_sub_branch=True` (which `_ParallelWorker` does pass) still
matters, but only as bookkeeping: it gives each fan-out branch its own event branch so
resumption and checkpointing can tell them apart, not as a content-visibility control —
`include_contents="none"` already made that moot for `topic_researcher`.

**A task-less run is still a capability of the workflow as a whole, not of one node.**
`project_coach` cannot call any node of `research_workflow` as a tool mid-conversation; the
trigger is still `POST /api/sessions/{sid}/research` (`docs/04-api-contract.md`), accepted
on a task's session or on the project's own intake session (`taskId: null`). The learner's
`reason` is the thing being researched, forwarded to `research_planner` as its topic exactly
as a task's own description is — there is no separate mechanism for the taskless case, and
`reviewer_writer` still validates and writes a `taskId: null` report the same way it does a
task-scoped one, promoting nothing into any checklist.

**Read-only access to the originating session's own uploads reaches `research_planner`, not
every node.** `ResearchService.start_manual` and `RunExecutor._research` still call
`SessionService.list_attachments(principal, session_id)` on the *originating* session — a
scan of its stored events for `file_data` parts, deduplicated by `gs://` URI — and still
forward every one it finds as `context_attachments`. Since M9 those attachments are embedded
into `research_planner`'s opening message only: it is the node deciding what the sub-topics
even are, and it is also, by construction, the node `reviewer_writer` shares a conversation
with, so a file cited by the plan is still visible to the final write-up without being
re-attached anywhere. `topic_researcher`'s whole reason to exist is a context clean of
everything but its own sub-topic; handing it the learner's uploaded rubric would be handing
back the crowded context M9 exists to remove.

**Each node's own instructions:**

- `research_planner` — read the topic and its details, ask nothing (there is no learner to
  ask; this still runs headless), and decompose it into 3–5 sub-topics that are mutually
  distinguishable and, together, cover the research goal. No tools beyond ordinary text
  output — the schema *is* the sub-topic list, validated the same way `post_research_report`
  validates its own shape.
- `topic_researcher` — for its one assigned sub-topic: `search_agent("…")` for authoritative
  material, `fetch_url` on the 2–4 most promising results to confirm they actually cover the
  sub-topic (title-based selection is how bad reading lists happen), and, if
  `prefs.allowVideos`, `youtube_find_by_duration(query, max_minutes=…)` — note there is no
  per-sub-topic budget to filter against yet, so this call asks for candidates rather than a
  single best fit, leaving the trim to `reviewer_writer`. Returns its ordered list of
  references with per-item estimated durations as plain output, not a tool call — there is
  nothing yet for it to post.
- `reviewer_writer` — check the combined sub-topic reports against the research goal;
  **deduplicate** a source repeated across two reports rather than listing it twice;
  **merge** sub-topics that turned out to overlap, keeping the union of what they found
  rather than either report's duplicate coverage of the shared part; then organize the
  result into `required[]` sized to fit `budgetMinutes` and everything else into
  `optional[]`, exactly as the single agent used to. Every required item still needs a `why`
  written in the second person and in the learner's own terms — it renders as the item's
  `shortDescription` — and `required[]`'s order is still the order the work happens in:
  reading before the exercise that uses it, setup before the thing being set up. Then calls
  `post_research_report`, unchanged: same schema, same budget validation, same promotion of
  `required[]` into `tasks/{id}.items[]`.

**No node of the research pipeline has a board-mutating tool.** `research_planner` and
`topic_researcher` have no tools that write anything at all; `reviewer_writer` has exactly
`post_research_report`. Adding, splitting, and reordering tasks still belong to
`propose_tasks`, a separate step with its own bounded budget, downstream of the whole
pipeline rather than a member of it. That separation is what
[10-risks.md](10-risks.md#r7--prompt-injection-via-fetched-pages-and-uploads) means by "tools
that mutate state are unavailable to every node of the research pipeline except
`post_research_report`, which only `reviewer_writer` calls" — and it matters more than it
did before M9: `topic_researcher` is now the node with fetched web pages in its context, run
3–5 times per job instead of once. **Since a later M9 change, that sentence is true of
`write_study_plan` too** — see immediately below.

#### The taskless case: `task_proposer` and `plan_tailor` replace `reviewer_writer`

`reviewer_writer` sizes everything the fan-out found into **one** `budgetMinutes` — correct
for a task-scoped run, where the budget is that one task's estimate, but wrong for a
*taskless* run (`taskId: null`, the project coach's own conversation): a learner's whole
goal is usually several tasks, not one report squeezed into one sitting. For the taskless
case only, `build_roadmap_workflow` (`agents/research_workflow.py`) replaces the last node
with four: the `topic_researcher` fan-out (shared with `research_workflow`), then
`research_findings`, then `task_proposer_scope`, then `task_proposer` -> `plan_tailor`.

| Node | Kind | Reads | Writes |
| --- | --- | --- | --- |
| `research_findings` | Plain Python function, `node()`-wrapped — no model call | `research_planner`'s sub-topic list (`SUBTOPICS_KEY`, its own `output_key`) and the fan-out's aggregate (its own `node_input` — `research_findings` sits directly after the fan-out so this is guaranteed) | The two zipped together, one object per sub-topic, to `RESEARCH_FINDINGS_KEY` |
| `task_proposer_scope` | LlmAgent, no tools, no output schema | The conversation so far (`include_contents="default"`) — the opening roadmap request plus `research_planner`'s own turn, and nothing from the fan-out's own branches (below) — and `{PREFS_KEY}` | Plain text to `TASK_PROPOSER_SCOPE_KEY`: the roadmap request and preferences, rewritten with the learner's total time budget for the whole roadmap left out |
| `task_proposer` | LlmAgent, `output_schema=ProposedTaskCollection`, no tools | `TASK_PROPOSER_SCOPE_KEY` and `RESEARCH_FINDINGS_KEY` only — `include_contents` is left at the `single_turn` default, `"none"`, so neither the raw roadmap request nor `research_planner`'s own turn reaches it | Nothing to Firestore. Its structured text is the hand-off, read by `plan_tailor` the same way `research_planner`'s is read by `reviewer_writer` today |
| `plan_tailor` | LlmAgent, `tools=[write_study_plan]` | `task_proposer`'s turn too (`include_contents="default"` reads the *whole* prior session, not just the immediate predecessor — so this is the one node downstream of `task_proposer_scope` that still sees the raw roadmap request, budget included), plus the project's board (`{BOARD_KEY}`) and completed-task history (`{OUTCOMES_KEY}`) — both already written into state by the shared `PromptBuilder`, no new context plumbing needed | Exactly one `write_study_plan` call: the pipeline's one write, same rule `reviewer_writer`/`post_research_report` follows |

**`task_proposer_scope` sits *after* the fan-out, not between `research_planner` and it —
load-bearing, not cosmetic.** `Workflow`'s edges are a sequential chain: each node's own
output becomes the *next* node's `node_input`. The fan-out's `node_input` has to stay
`research_planner`'s `list[str]` output, chain-adjacent, or `_ParallelWorker._run_impl`
(`google/adk/workflow/_parallel_worker.py`) — which wraps a non-list `node_input` in a
single-item list rather than rejecting it — silently turns "one `topic_researcher` branch
per sub-topic" into "exactly one branch, fed whatever node sits there as if it were the one
sub-topic", with no construction-time error to catch it. Placing `task_proposer_scope`
after the fan-out instead costs it nothing: `include_contents="default"` content-building
filters by branch (`flows/llm_flows/contents.py::_is_event_belongs_to_branch`), and a
main-branch node is never a descendant of a `topic_researcher` sub-branch
(`use_sub_branch=True`) — so it still sees only the opening message and
`research_planner`'s own turn, the same mechanism `reviewer_writer` already relies on from
the same position, never the fan-out's own branches, regardless of where after the fan-out
it sits. `research_findings` has the mirror-image constraint: it must sit *directly* after
the fan-out, since its own `node_input` has to be the fan-out's aggregate.

**Why `task_proposer_scope` and `research_findings` exist at all.** `task_proposer` used to
read the conversation the same way `plan_tailor` still does — `include_contents="default"`,
seeing the opening roadmap request (`ResearchService.start_roadmap`'s `reason`, prose,
whether typed free-hand or rendered from a `RoadmapBrief`) directly. That request is where
the learner's *total* time budget for the whole roadmap lives — there is no structured
field for it once a run starts, only that prose — and a `task_proposer` that already knows
the total starts trimming and merging tasks to fit its own guess at it, before `plan_tailor`
— the node whose actual job that is, once every task has been proposed and the shape of
the whole roadmap is known — gets a say. Fixing that meant `task_proposer` could no longer
read the conversation at all (there is no way to redact one fact from a replay), which cost
it two things `include_contents="default"` used to hand it for free: the roadmap's own
goal/topics (nothing else renders them for a taskless run — `{FOCUS_KEY}` is
board/task-shaped and empty here) and `research_planner`'s own sub-topic breakdown
(`TopicFindings` carries only each sub-topic's `items`, never the sub-topic string itself).
`task_proposer_scope` and `research_findings` restore exactly those two things, deliberately,
as explicit state — `TASK_PROPOSER_SCOPE_KEY` and `RESEARCH_FINDINGS_KEY` — rather than as
an accidental side effect of reading everything that happened to be in the session.

`task_proposer` groups the fan-out's findings into several `ProposedTask`s, each sized to
the learner's *preferred* task length (the "Default task length" line inside
`TASK_PROPOSER_SCOPE_KEY`, carried through from `{PREFS_KEY}` unchanged) rather than one
combined budget — with `required[]`/`optional[]` per task, same item shape as a
`ResearchReport`'s, plus `prerequisiteTasks` linking one proposed task to another.
`plan_tailor` does not research further; it decides, using the board and history it can now
see that neither upstream node could, whether each proposed task belongs (`include`), is a
deep dive (`additional`), is already covered (`exclude`), or does not fit the goal
(`reject`) — with a `why` for every one, including the ones it drops — and where it sits
relative to the others. It reproduces `task_proposer`'s task list unchanged as part of its
own `write_study_plan` call rather than the code splicing the two turns' output together:
reading a finished turn's structured output back out of stored Firestore events is exactly
the machinery the single-step research ledger
([above](#workflow-as-a-turn-root-and-retrying-a-crash-mid-fan-out)) avoids needing at all —
a token cost here, not a correctness gap.

`write_study_plan` writes `projects/{projectId}/study_plans/{planId}`
([02-data-model.md](02-data-model.md#projectsprojectidstudy_plansplanid)) — a `StudyPlan`,
never a task. Turning one into board tasks is a separate, standalone tool,
`materialize_study_plan` (below), by design: the pipeline may still write exactly one kind
of document, and reshaping the board stays a deliberate, later act rather than something
that happens inside the same turn that read a fetched web page.

**Reachable through its own endpoint, not through `/research`'s dispatch.**
`ResearchService.start_manual`/`RunExecutor` still dispatch every run they start — taskless
or not — to `research_workflow`/`reviewer_writer`, so `POST /api/sessions/{sid}/research`
and `GET /api/runs/{runId}/report` behave exactly as they did before this section, and the
M8 golden e2e flow is unchanged. `build_roadmap_workflow` has its own caller instead:
`ResearchService.start_roadmap`, behind `POST /api/sessions/{sid}/roadmap`
([04-api-contract.md](04-api-contract.md#post-apisessionssidroadmap)) — the same
lease/ledger/fresh-session machinery as `start_manual`, dispatching `agent="roadmap"`
rather than reshaping `start_manual` itself to carry a second agent. A roadmap run is
distinguished from a research run by `steps[0].id` (`"roadmap"` vs `"research"` —
`ResearchService.ROADMAP_STEPS`/`MANUAL_STEPS`), since neither carries a field naming its
pipeline directly. `StartProjectRoadmap` (`apps/web`) is the button; the research view
renders its transcript (multiple named agents, one session) and, since this UI rework, a
dedicated panel for the `StudyPlan` too — `GET /api/runs/{runId}/plan`
([04-api-contract.md](04-api-contract.md#runs)) reads `study_plans` back, and
`StudyPlanView` (`apps/web/src/components/research/`) renders it: every proposed task as
its own `ProposedTaskCard`, `plan_tailor`'s decision shown as a corner chip
(`include`/`additional`/`exclude`/`reject`) with its `why` always visible, and the task's
required/optional material behind a disclosure. `GET /api/runs/{runId}/report` still 404s
for a roadmap run, since that endpoint reads `research_reports`, not `study_plans`.

**`materialize_study_plan` is wired into `project_coach`'s own catalogue**, alongside
`view_study_plan` (reads the most recent plan for the project) and `revise_study_plan`
(`project_coach`'s own re-tailoring — which proposed tasks to include, and where each
sits). The plan document `write_study_plan` posted itself stays immutable —
`StudyPlanView` still renders exactly what `plan_tailor` wrote, corner chip and all — and a
revision is a *copy*, `revisedFromPlanId` pointing back at the plan it replaces, never an
edit in place, so the original verdict stays legible against whatever a learner and the
coach later decide instead. `materialize_study_plan` itself now requires the learner's
confirmation, the same gate `update_project_plan` uses, since it is the act that actually
puts a plan's tasks on the board.

**Its confirmation dialog also offers "Also update project description"**, checked by
default when the project's own `description` is empty. `short_description`/
`long_description` on the plan are composed as a proposed roadmap, which is the wrong
register for a project's own description — so `materialize_study_plan` takes its own
`project_description` argument, a single factual sentence `project_coach` composes fresh
for the project rather than copying the plan's. The candidate is supplied on every call,
whatever the learner is expected to answer: the confirmation handshake does not consult
the model again between the call and the answer, so the sentence has to already exist by
the time the dialog shows the checkbox, and only the checkbox decides whether it is
applied (`UPDATE_PROJECT_DESCRIPTION_KEY` in the answer's payload, the same
restated-constant arrangement as `STOP_CONFIRMING_KEY`).

**Initiating a roadmap run is itself now something `project_coach` can do, not only the
`StartProjectRoadmap` button.** `write_roadmap_brief`/`read_roadmap_brief` let the coach
draft a structured `roadmapBrief` on the project (subject, time budget, specific topics,
additional notes — [02-data-model.md](02-data-model.md#projectsprojectid)) across several
turns, the same way an ordinary conversation gathers a plan before calling
`update_project_plan`. `propose_roadmap_brief` is the confirmation-gated handoff: approval
renders the brief into the free-text `reason` `ResearchService.start_roadmap` already takes
and schedules the run through the same lease/ledger/queue path
`POST /api/sessions/{sid}/roadmap` uses, then clears the draft. The manual button is
unchanged and reaches the identical `ResearchService.start_roadmap` call — this is a second
way to reach it, not a replacement.

**The confirmation dialog for `propose_roadmap_brief` renders `Project.roadmapBrief` —
the document `write_roadmap_brief` last stored — not the confirmation call's own
arguments.** The model is asked to reproduce the stored brief exactly, but that is an
instruction, not an enforced constraint, and the two are not the same claim: rendering
from the arguments would show the learner the model's restatement of the brief, where
rendering from the document shows them the brief. `attachments` (display names the
learner referenced) is the field this distinction matters most for — the dialog turns it
into an editable checklist rather than a read-only list, covered in its own paragraph
below, which only makes sense starting from what the brief actually names, not a value the
model happened to echo. `start_roadmap`'s own `attachment_names` (`services/research.py`)
narrows `_create_and_enqueue`'s upload carry-over to that same list; `None` (every other
caller — the manual button, task-scoped research) keeps the old "every upload the
conversation has seen" behaviour, since a task's or an intake session's conversation is
already scoped to one topic and a project coach's ongoing conversation is not.

**Neither tool trusts a name it is given — each is checked against the session's actual
uploads before it is stored** (`DomainTools._validate_attachment_names`). Found from real
use, not anticipated in the original design: a model asked to attach an uploaded PDF can
name it by a title it inferred from reading the document rather than the filename it was
actually shown for it, and a name matching nothing would otherwise be accepted, stored,
and then simply drop out at `start_roadmap`'s own matching — invisible to the model that
made the mistake and only discoverable by the learner noticing an absent chip. A
validation failure is a `ValidationProblem` naming exactly which of the given names are
unmatched, plus the real list of what is attached, so the model can retry correctly in
the same turn rather than the brief silently omitting the file.

**Validation stops a hallucinated name; it does nothing about a model that never
considers attachments in the first place.** That gap survived the instruction fix above
in real use too — a model can go several `write_roadmap_brief` calls without ever
weighing whether an uploaded file belongs on the brief, especially once it is busy
reasoning about subject and time budget instead. Two independent responses, neither
trusting instruction text alone to be enough a second time:

- **A one-time nudge in the tool's own result.** `write_roadmap_brief` adds an
  `availableAttachments` field — the conversation's own attachment names — when, and only
  when, this is the *first* call for the brief (`Project.roadmapBrief` was `None` before
  this write) and it named none while the conversation has some. A later call that still
  names none gets no nudge: by then the silence reads as a decision, not an oversight, and
  repeating the hint would be nagging about a choice already made. This is a nudge, not a
  guard — nothing stops the model from ignoring it — but it puts the fact in front of the
  model on the one call where raising it does not yet read as second-guessing.
- **A checklist in the confirmation dialog itself**, so the learner does not have to rely
  on the model noticing at all. See the frontend section below and
  [06-frontend.md](06-frontend.md#task-workspace-projectsprojectidtaskstaskid) — the
  learner can tick or untick any conversation attachment right in the dialog, and the
  server applies that selection deterministically, overriding the model's own `attachments`
  argument (`DomainTools._confirmed_attachments`, keyed on `CONFIRMED_ATTACHMENTS_KEY` in
  the confirmation answer's payload). This is the one that actually closes the gap: a
  learner does not need the model to get it right, or even to try, to end up with the
  correct file on the run.

#### Built on ADK's `Workflow`, not `SequentialAgent` / `ParallelAgent`

Verified against the installed `2.7.0` source, not published docs, the same rule this
project applies to every ADK surface: `google/adk/agents/sequential_agent.py`,
`parallel_agent.py`, and `loop_agent.py` are each decorated `@deprecated('… in favor of
Workflow …')`, and `Workflow` is exported at `google.adk`'s own top level —
`__all__ = ['Agent', 'Context', 'Event', 'Runner', 'Workflow']` — beside the four things
every turn in this project already depends on. `research_workflow` is this project's first
use of it; `autonomous_workflow` itself stays a `SequentialAgent` for now; see the open
question below about what that means for hosting a `Workflow`-rooted step.

The graph-based `Workflow` (`google/adk/workflow/_workflow.py`) is what gives this pipeline
its two load-bearing properties:

- **Runtime-sized parallel fan-out**, for a sub-topic count the planner decides (3–5) rather
  than a fixed number of branches declared up front. `google/adk/workflow/_parallel_worker.py`
  wraps a node so that it runs once per item of a list produced at run time, bounded by
  `max_parallel_workers` — exposed via `node(..., parallel_worker=True,
  max_parallel_workers=…)` (`google/adk/workflow/_node.py`). `topic_researcher` is declared
  once and fanned out over `research_planner`'s own output list.
- **Per-node context isolation without a second session service call.** Covered above — a
  standalone node's default `single_turn` mode forces `include_contents="none"`, which is
  what stands in for "clean session" here, and it is why M9 needed no new collection and no
  new field on `sessions/*`.

#### LLM throttling: at most one inference in flight per research job

A 3–5-way fan-out is exactly the shape that used to arrive as one sequential agent's tool
calls and now arrives as up to five agents each wanting a model turn at roughly the same
moment — a burst this project's model traffic never produced before M9, and the kind of
burst Vertex answers with `429 RESOURCE_EXHAUSTED`. The fix is a small in-memory limiter, not
a data-model change: an `asyncio.Semaphore(1)` per run, held around the model-call site for
the duration of one inference and released immediately after, keyed by the run id already
threaded through `temp:coach_run_id` for `post_research_report`
([05-autonomous-runs.md](05-autonomous-runs.md#execution-semantics)). Attached via a
`before_model_callback` / `after_model_callback` pair on `research_planner`,
`topic_researcher`, and `reviewer_writer` only — the interactive agents' traffic is shaped by
the human waiting for a reply, not by a fan-out this project introduced, and throttling it
would only make the chat feel slower for no gain. The limiter lives in a process-local dict
keyed by `runId`, entries dropped when the run's step ends; a second instance running a
different project's research gets its own independent semaphore, which is deliberate — the
goal is shaping one job's own burst, not rate-limiting the process globally (a *global* cap
would also throttle unrelated interactive turns sharing the instance, which is not the
problem this exists to solve). `max_parallel_workers` on the `topic_researcher` fan-out is a
second, independent knob at the ADK level and is not a substitute for this one: it bounds how
many `topic_researcher` branches *execute* at once, not how many model calls two *different*
nodes (a lingering `topic_researcher` and an already-started `reviewer_writer`, say) might
happen to overlap.

#### M10: a process-wide token ceiling per Vertex model

`ModelThrottle` above deliberately stops at shaping one research job's own burst — a
*global* cap was explicitly ruled out there because it would also slow down interactive
chat for no reason tied to the fan-out. That reasoning holds for concurrency, but not for
Vertex's own token-per-minute ceiling on the model itself: that limit is real, shared by
every caller against the same model regardless of which agent placed the call, and
observed in practice as `429 RESOURCE_EXHAUSTED` from ordinary interactive turns, not only
from `research_workflow`'s fan-out. `TokenRateLimiter` (`integrations/model.py`) is the
second, independent throttle this calls for: an in-memory sliding window over
`total_token_count` from every completed call to the configured model — `TOKEN_WINDOW_SECONDS`
(2 minutes, a constant so it can be retuned against the real observed window) and
`TOKEN_WINDOW_LIMIT` (200,000) — that makes the next call wait rather than reject once the
trailing window is already at capacity.

Attached beneath every agent this process builds, not through a `before_model_callback`
the way `ModelThrottle` is: `ThrottledLlm` wraps the model itself at
`integrations/model.py::build_model`, around `generate_content_async`, so `project_coach`
and `task_teacher` are covered on the same terms as every `research_workflow` node and the
`search_agent` sub-agent it calls, none of which carry `ModelThrottle`'s own callbacks
today. One `TokenRateLimiter` is built once per process (`api/deps.py`, beside
`ModelThrottle`) and threaded into `RunnerFactory`, so the five runners it builds — each
`build_model()` call constructs its own `Gemini` instance — share one window rather than
five independent ones. The stub backend is returned unwrapped: it never calls a real
model, so there is no ceiling to respect, and the e2e harness wants its own scripted
timing, not an extra wait.

This does not reserve budget for a call in flight — a call's own token cost is unknown
until it returns — so it bounds the *rate* of completed usage rather than an instantaneous
hard ceiling: several calls admitted while the window still had room can still land
concurrently and only be counted once they finish. Combined with `ModelThrottle`'s
per-run concurrency cap and the retry/backoff on `generation_config`'s `http_options`
(`integrations/model.py`), this is the third of three independent layers between this
project's model traffic and a real `429`, not a replacement for either of the other two.

#### `Workflow` as a turn root, and retrying a crash mid-fan-out

**`Workflow` is a valid turn root, the same mechanism every other agent choice already
uses.** `runners.py`'s `run_async` has a dedicated branch for it —
`if isinstance(self.agent, BaseNode) and not isinstance(self.agent, BaseAgent): …
self._run_node_async(…)` — taking the same `user_id` / `session_id` / `new_message` /
`state_delta` / `run_config` arguments `TurnService._generate` already passes for an
`LlmAgent` turn. `Workflow` is a `BaseNode`, not a `BaseAgent`, so it lands here rather than
the chat/task branch. `research_workflow` needs no change to `autonomous_workflow`'s own
shape or to `TurnService._generate`'s event loop, which already treats `Event`s generically
(`event.partial`, `.get_function_calls()`, `.get_function_responses()`, `.usage_metadata`)
regardless of which kind of root produced them — it is just a different value in
`RunnerFactory`/`_RUNNERS`, once per turn.

**The ledger keeps a single `research` step, not one per pipeline node.** A per-node ledger
would need `research_planner`'s sub-topic list and the aggregated `topic_researcher` reports
to cross from one `RunExecutor`-dispatched turn into the next, and the only channel
`RunExecutor` has for passing data into a turn — `temp:` session state
([05-autonomous-runs.md](05-autonomous-runs.md#execution-semantics)) — is trimmed before
Firestore persistence, so it does not survive between two separate `Runner.run_async` calls
into the same session. The alternative, reading a finished turn's *structured node output*
back out of stored Firestore events, is exactly the kind of ADK event shape CLAUDE.md's own
working agreement says to dump and verify against a running turn rather than reason about in
the abstract — and not a cost worth paying here. One `Workflow`, one turn: `RunExecutor`
keeps the single `research` step it had before M9, now dispatching `research_workflow`
instead of `research_agent`
([05-autonomous-runs.md](05-autonomous-runs.md#execution-semantics)).

**A crash mid-fan-out is safe to retry but not cheap: `research_planner` runs again.**
`Workflow`'s own SETUP phase (`replay_manager.scan_workflow_events`) does fast-forward
through nodes an invocation being *resumed* finds already completed, but
`RunExecutor`'s retry is a brand-new `TurnService.start` call with a fresh ADK
`invocation_id`, not a resume of the original one — `_build_event_index`
(`workflow/utils/_replay_manager.py`) filters the session's stored events down to the
*current* `ic.invocation_id` before scanning them, so nothing threads the first attempt's id
through for the second to pick up. What resume-at-cursor still guarantees is safety, not
savings: a crash mid-fan-out retries the whole `research` step — `research_planner` runs
again — but the run still completes, and `report_{runId}` keying still means one report
rather than two
(`tests/test_run_executor.py::test_a_crash_mid_fan_out_retries_the_whole_research_step_safely`).
Threading a resumable `invocation_id` through `RunExecutor`/`TurnService.start` would recover
the planner's one call on a retried run and is a reasonable follow-up, not needed for
correctness.

### `autonomous_workflow` (SequentialAgent)

Steps are individually checkpointed by the run ledger, so each is separately resumable:

| # | Step | Kind | Notes |
| --- | --- | --- | --- |
| 1 | `select_next_task` | code | Deterministic: a task the learner **queued** (`researchStatus == "pending"`) if there is one, oldest `researchRequestedAt` first; otherwise the lowest `order` among `draft`/`not_started`/`in_progress`, skipping `completed`, `discarded`, `postponed`, and unexpired `postponed_until`. No LLM. A run that took the project *because* something was requested has to research that thing ([05-autonomous-runs.md](05-autonomous-runs.md#candidate-selection-and-guards)). |
| 2 | `research` | `research_workflow` (since M9; was `research_agent`) | Skipped if `task.needsResearch == false`. One turn drives the whole `research_planner` → `topic_researcher` × 3–5 → `reviewer_writer` graph to completion — `Workflow` has no "run one node and stop" primitive, so the ledger keeps one step ([above](#workflow-as-a-turn-root-and-retrying-a-crash-mid-fan-out)). A crash mid-fan-out is safe to retry — no duplicate report — but not cheap: the retry is a new turn with a fresh ADK `invocation_id`, so `Workflow`'s replay (scoped to the *current* invocation) does not skip `research_planner`, and it runs again. |
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
| `post_research_report` | — (`reviewer_writer`, since M9; formerly `research_agent`) | `(summary, required[], optional[])` — `task_id` is read from the invocation, not an argument, and may be `null` | validates `Σ required.minutes ≤ budget`, assigns `itemId`s, writes the report, and — only when the invocation names a task — promotes `required[]` into `tasks/{id}.items[]` in the same transaction. A task-less call (M8: research about the project as a whole) writes the report and stops there; there is no checklist to promote into |
| `write_study_plan` | — (`plan_tailor`, `build_roadmap_workflow`) | `(title, short_description, long_description, memo, proposed_tasks[], plan[])` | validates every proposed task's items and every plan entry (unique `taskSlug` covering every proposed task, known slugs in `prerequisiteTasks`/`after`, `relevance` 0–4, a real `decision`, a non-empty `why`), then writes one `StudyPlan` ([02-data-model.md](02-data-model.md#projectsprojectidstudy_plansplanid)). Never touches the board |
| `write_roadmap_brief` | `project_coach` | `(subject, time_budget, specific_topics?, additional_notes?, attachments?)` | `subject`/`time_budget` non-empty; upserts the project's one in-progress `roadmapBrief` draft. `attachments` names files the learner referenced (display names), not every upload the conversation has seen — **each name is validated against the session's own uploads and the call is refused, with the real list, if any name matches nothing**. The result also carries `availableAttachments` — the conversation's own attachment names — on the first call for a brief, if that call named none and the conversation has some, so a model that never considered them gets one nudge to reconsider |
| `read_roadmap_brief` | `project_coach` | `()` | owner; `null` if no draft, or the last one was already used to start a run |
| `propose_roadmap_brief` | `project_coach` | `(subject, time_budget, specific_topics?, additional_notes?, attachments?)` | **requires user confirmation**. The confirmation dialog renders the project's stored `roadmapBrief` document, not these arguments — see the note below. **Its own attachment checklist, if the learner edits it, overrides `attachments` deterministically** (`CONFIRMED_ATTACHMENTS_KEY` in the answer's payload) — the model's argument is only the starting point the dialog shows, not the last word. Approval re-stores the brief, renders it, and calls `ResearchService.start_roadmap` with `attachment_names=` the resolved list — the same lease/ledger/queue path `POST /api/sessions/{sid}/roadmap` uses — then clears the draft |
| `view_study_plan` | `project_coach` | `()` | owner; returns the most recently written plan for the project — `plan_tailor`'s own write or a later `revise_study_plan` copy, whichever is newer |
| `revise_study_plan` | `project_coach` | `(plan_id, plan[])` | same `plan[]` validation as `write_study_plan`'s; writes a **new** `StudyPlan` (`revisedFromPlanId` pointing at `plan_id`) rather than editing it, so the original verdict stays legible. Refused once `plan_id` has already been materialized |
| `materialize_study_plan` | `project_coach` | `(plan_id, decisions?, project_description)` | **requires user confirmation** — the study-plan analogue of `update_project_plan`'s gate. Creates a real board task — with `required[]` promoted into `items[]` — for every plan entry whose `decision` is `include` or `additional` (the default), in `prerequisiteTasks`/`after` order; `exclude`/`reject` create nothing. Idempotent: a plan already materialized returns its first set of created tasks rather than making a second one. `project_description` is a one-sentence, factual candidate for `Project.description` — always supplied, applied only if the confirmation dialog's "Also update project description" checkbox comes back checked |

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
