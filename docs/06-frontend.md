# Frontend

Vite + React 19 + TypeScript, React Router (client-side only, no SSR), Tailwind CSS +
shadcn/ui, TanStack Query + Zustand, Vitest + Playwright.

Formatting is **Prettier**; ESLint only lints. The two do not overlap —
`eslint-config-prettier` sits last in the ESLint config and turns off every rule with an
opinion about whitespace — so a disagreement between them is impossible rather than
merely unlikely. See [07-infra-deploy.md](07-infra-deploy.md#formatting-and-linting) for
what runs, in which order, and why the order matters.

## Routes

| Path | Screen |
| --- | --- |
| `/login` | Google sign-in (Identity Platform popup) |
| `/` | Project list — cards with progress, open minutes, "coach updated this" badge |
| `/projects/:projectId` | **Task board** — ordered task list, filters, next-up highlight |
| `/projects/:projectId/tasks/:taskId` | **Task workspace** — split view: task detail + checklist on the left, session chat on the right |
| `/projects/:projectId/research/:runId` | + M8. **Research view** — a single pane, toggled between the research session's own transcript and its results (the final report, or a roadmap run's study plan). See [Research view](#research-view-projectsprojectidresearchrunid) below |
| `/projects/:projectId/settings` | Project preferences (task duration, research depth, videos), with the same "← Back to the board" link the task workspace and the research view carry |
| `/settings` | Account (display name, editable — M8-quotas), global prefs, appearance ([theme](#theme-light-dark-system)), usage & plan + coupon redeem (M8-quotas) + **"What your coach knows about you"** (learner profile, editable) |

Routes are lazy-loaded per screen. Auth is a route guard resolving the Identity Platform
auth state before first render to avoid a login flash.

Identity Platform is reached through the `firebase/auth` JS SDK — the client library for
`identitytoolkit` — configured with the project's Identity Platform settings. The SDK
silently refreshes the ID token before its one-hour expiry and surfaces it via
`onIdTokenChanged`, so the fetch wrapper and the WebSocket ticket flow always attach a live
token without a hand-rolled refresh loop.

## State management split

The brief asks for a sensible decision here. The rule:

> **TanStack Query owns anything the server can also change. Zustand owns anything that
> exists only in this tab.**

### TanStack Query (server state)

| Query key | Data |
| --- | --- |
| `['me']` | Profile, global prefs, learner profile |
| `['projects']` | Project list |
| `['project', id]` | Project + effective prefs |
| `['tasks', projectId, filters]` | Board, parents with nested subtasks |
| `['task', taskId]` | Task detail + latest report |
| `['session', sessionId, 'events']` | Infinite query over `seq`, for transcript hydration |
| `['run', runId]` | Autonomous/manual run status (`GET /api/runs/{runId}`) |
| `['project', projectId, 'runs']` | Recent runs — backs the "Updated by your coach" banner and its undo |

Configuration: `staleTime: 30_000` for board data (the WebSocket tells us when it is
actually stale, so aggressive refetch-on-focus is unnecessary noise), `gcTime: 5 min`,
retry with backoff except on 4xx.

Optimistic mutations for the interactions that must feel instant: complete task, postpone,
reorder (drag-and-drop), hide-completed toggle. Each uses
`onMutate` → snapshot → patch cache → `onError` rollback → `onSettled` invalidate. Reorder
patches the fractional index locally using the same midpoint algorithm as the server, so
the optimistic order matches the confirmed order.

### Zustand (client-only state)

| Store | Contents |
| --- | --- |
| `useStreamStore` | Per-`turnId`: one or more author-keyed segments (accumulated text + tool-call chips each), `lastSeq`, status. The hot path. |
| `useSocketStore` | Connection state, ticket refresh, backoff, resume queue, presence heartbeat |
| `useComposerStore` | Draft text and pending attachments per session (survives navigation) |
| `useBoardUiStore` | `hideCompleted`, `hideDiscarded`, collapsed parents, selection — persisted to `localStorage` |
| `useThemeStore` | `pref` (`light \| dark \| system`) and the `resolved` value — persisted to `localStorage`. See [Theme](#theme-light-dark-system) |

**Why streaming tokens must not go through the Query cache:** a delta arrives every few
tens of milliseconds; writing each into `queryClient.setQueryData` invalidates observers
and re-renders every consumer of that key. The stream buffer lives in Zustand with a
selector subscribed only by the message bubble component. On `turn_complete`, the buffer
is flushed once into the Query cache (`setQueryData(['session', sid, 'events'], append)`)
and cleared. One handoff, one re-render of the transcript.

**A turn's content is one or more segments, one per author, not one flat buffer.** An
ordinary chat turn has exactly one — the root agent never changes mid-turn — but a
`research_workflow`/`build_roadmap_workflow` turn is several named agents
(`research_planner`, `topic_researcher`, …) writing into the *same* turn
(docs/03-agent-design.md#the-research-pipeline-since-m9), and the persisted transcript
already renders one bubble per stored *event*, each with its own author
(`lib/transcript.ts`'s `toMessages`). `delta`/`tool_call` frames carry `author`
(`04-api-contract.md#websocket-protocol-ws`) so the live buffer can draw the same
boundary: a new segment starts the instant a frame's author differs from the current
last segment's. Before this, every delta for a turn landed in one `text` string
regardless of author, so `research_planner` finishing and `topic_researcher` starting
looked like one bubble quietly growing rather than two separate messages — invisible for
a single-author chat turn, wrong the instant a turn had more than one.

**A confirmation prompt must be gated on the live turn existing, not on it still
`running`.** `SessionPane`'s `pending` used to be `streaming ? null :
pendingConfirmation(events)` — but the turn that *answers* a confirmation goes `running`
→ `complete` before its own `events.refetch()` (fired by the same handoff that clears the
buffer) has actually landed, and `pendingConfirmation` reading the still-stale cache in
that gap finds the same request unanswered: the confirmation buttons flash back onto
screen for the instant between the answering turn completing and the refetch resolving,
then disappear again once it does. Gating on `live` itself — cleared only inside that
same refetch's `.then()` — closes the gap, because `events` is guaranteed fresh by the
time `pending` is ever computed from it again.

### The bridge

`board_update` frames call `queryClient.invalidateQueries({ queryKey: ['tasks', projectId] })`.
The server tells us *what* changed; Query decides *when* to refetch. This gives live board
updates while the autonomous agent works, with a single auth path and no Firestore client
SDK in the browser.

**The handler is registered on the socket, not passed to it.** `getSocket` is a singleton,
so its constructor arguments belong to whoever calls it first — and React runs child
effects before parent ones, so a direct load of the task workspace has the *page* build
the socket for its presence heartbeat before `AppShell`'s `useCoachSocket` runs. Passed as
a dependency, the invalidation callback was dropped for the lifetime of that tab.

**And a completed turn invalidates the board as well.** That is not a duplicate: frames
are not checkpointed, so a client whose socket was down while a tool ran gets its text
back on resume and never hears that the board moved — and from M5 a run executing on
another instance never sends it one at all. The push is what makes the board feel live;
the turn-complete invalidation is what makes it correct.

## WebSocket client

A single module owns the socket:

- Connect on auth, with ticket fetch → connect → subscribe to any active turns.
- Reconnect with exponential backoff + jitter, capped at 30 s; a fresh ticket per attempt.
- On reconnect, for every turn in `useStreamStore` with `status === 'running'`, send
  `{type:'resume', turnId, lastSeq}`. Deltas with `seq <= lastSeq` are dropped, so replay
  overlap is harmless and exactly-once rendering is guaranteed by sequence number, not by
  luck.
- Presence heartbeat every 30 s while a task workspace is focused; stopped on
  `visibilitychange` to hidden after 2 min, so a forgotten background tab doesn't block
  the autonomous agent forever.
- Frames are validated with Zod at the boundary; unknown `type` is ignored forward-compatibly.

## Key screens

### Task board (`/projects/:projectId`)

- Ordered list of top-level tasks. Whatever `project.nextUpTaskId` names is visually pinned
  as "Next up." That pointer is derived rather than enforced from M4
  ([02-data-model.md](02-data-model.md#task-state-machine)), so the pin can sit on an
  `in_progress` task while other tasks are also `in_progress` — the board no longer implies
  the learner has exactly one thing open.
- **The project's intake conversation sits beside the board** (added at M3). The session
  `POST /api/projects` opens has `taskId: null` and no route of its own, and the board is
  the screen a new project lands on — so the learner watches cards appear as the coach
  proposes them, which is what `board_update` is for. It is the same `SessionPane` the
  task workspace renders; `POST /api/projects/{id}/session` resolves it on a later visit.
- Card shows: title, estimated duration chip, state badge, research status (a small
  "materials ready" indicator), and `origin: agent` badge when the coach created it. A leaf
  task with items shows "2 of 5 done" in the same slot a parent shows its subtask rollup —
  the two are the same idea and never both apply. Beside that progress row, a leaf card
  also shows `ItemKindStrip` (`components/task/item-kind.tsx`,
  [more below](#task-workspace-projectsprojectidtaskstaskid)) — icons only, one per kind
  present among the task's items, grouped with a count ("2x[book] [play]") — since
  `estimate` above already carries the task's duration and the strip's own optional
  duration slot would just repeat it.
- **A `draft` card reads as "no plan yet", not as blocked.** The badge is muted and the card
  offers "Research this task" alongside the ordinary Start action, because `draft` is a
  state the learner may work straight out of ([02-data-model.md](02-data-model.md#task-state-machine)).
- **A task with research queued shows a "Starts soon" badge**, in the same slot as the
  "materials ready" indicator and reading from `researchStatus == "pending"`. The board is
  where a learner queues several tasks in a row and then wants to see what is waiting, so
  the indicator has to be here and not only on the screen that set it. It is a badge rather
  than a control: queueing and cancelling both live in the workspace, where the rest of the
  research affordances are.
- **Parent cards show `rollup.subtaskCount` and `rollup.totalEstimatedMinutes`** ("4
  subtasks · 2 h 30 m") with a progress ring for `completedSubtasks`. Expanding reveals
  subtasks inline.
- **The board shows one "Latest research" card, added at M8, separate from the task
  list.** It is the project's most recent research job — the first entry of
  `GET /api/projects/{id}/runs` whose steps include `research`, whether that job is about
  one task or, since M8, the project as a whole — rendered as a brief description (the
  report's `summary`, truncated to a line or two, or "Your coach is researching…" /
  "Couldn't finish — try again" while it has none yet) plus its creation date and status.
  Clicking it opens the [research view](#research-view-projectsprojectidresearchrunid).
  This is the board-level echo of the same card the task workspace shows for one task's
  own research, below — same component, different feed.
- **Beside it, "View previous research (N)"** reveals the rest of `GET
  /api/projects/{id}/runs`, each rendered as the same card. Collapsed by default and
  fetching nothing until opened — a project with a long research history costs no extra
  requests until the learner actually asks to see it. This is what closed the scope cut
  recorded at the end of M8 ([09-roadmap.md](09-roadmap.md#status-after-m8)): reports
  already accumulated in Firestore, and this is what makes the older ones reachable from
  the UI rather than only by URL.
- **Two collapsed prompts, side by side, below the research card: `StartProjectResearch`
  and `StartProjectRoadmap`.** Both are a single button until pressed, then a textarea plus
  Cancel/submit — a much rarer action than opening a task, so an always-open form would
  compete with the board for attention on every visit. They hit different endpoints
  (`POST /api/sessions/{sid}/research` vs `/roadmap`) and start different pipelines
  (`reviewer_writer` vs `task_proposer`/`plan_tailor`,
  [03-agent-design.md](03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer)),
  kept as two buttons rather than one form with a mode switch so which one was pressed
  stays legible from the UI itself. Both navigate to the same
  [research view](#research-view-projectsprojectidresearchrunid) on success.
- Filters: `Hide completed` (default **off**), `Hide discarded` (default on), `Hide
  postponed` (default off). Persisted per project in `useBoardUiStore`. `Hide completed`
  defaults off rather than on — a freshly completed task vanishing from the board the
  moment it's done reads as the task disappearing rather than as progress, which is the
  opposite of what finishing something should feel like to a learner.
- Drag-and-drop reordering (dnd-kit), optimistic, disabled while an autonomous run holds
  the project lease (the UI shows "Your coach is working on this project…" from
  `run_status`).
- Row actions: start, complete, postpone, postpone until… (date picker), discard. **Not
  split** — `POST /api/tasks/{id}/split` and its "Split into subtasks…" action were removed
  after M4. A subtask is created one at a time now, by the coach through `add_subtask` or by
  hand through `POST /api/projects/{id}/tasks` with a `parentTaskId`.

### Task workspace (`/projects/:projectId/tasks/:taskId`)

**Above both panes, always visible — breadcrumbs and a compact info strip**, added
shortly after M8. Breadcrumbs (`Projects / {project title} / {task title}`) sit beside the
existing "← Back to the board" link rather than replacing it — the two answer different
questions, "where can I go" and "where am I". Below them, a narrow strip echoes a
`TaskCard` row: the estimate chip, the state badge, and either the subtask rollup or the
item-completion count, whichever applies — **with no title**, since the breadcrumb right
above it already carries one and repeating it would be the same fact twice in adjacent
elements. The strip is not part of the collapsible column below; it is the thing that
stays on screen when that column is hidden.

Two panes below that, stacked on mobile.

Left — **task detail, collapsible**:
- A "Hide details" / "Show details" toggle, remembered per task
  (`useWorkspaceUiStore`) and expanded by default always — nothing collapses on its own.
  The column is reference material once a learner is mostly working a checklist through
  conversation with the coach rather than by clicking checkboxes directly, and hiding it
  hands that width to the chat; the info strip above keeps "how far along is this"
  visible either way.
- Description. The estimate and state badges moved to the always-visible info strip
  above, since M8, rather than being duplicated in both places.
- **A composite task shows its subtasks as cards**, between the detail and the research
  report. `GET /api/tasks/{id}` already returns `subtasks[]`, so this costs no request.

  **A subtask has no route of its own, and that is the product decision rather than a
  gap.** The parent's session is where subtasks are worked through — one conversation
  covers the whole piece of work, and giving each subtask a workspace would fragment it
  into four transcripts that each know a quarter of the story. So the cards are a
  checklist inside this screen: title, estimate, state badge, and the same row actions
  the board gives a subtask (complete, postpone, discard). Nothing here navigates.

  Above them sits the same `rollup` pair the board's parent card shows — a progress ring
  over `completedSubtasks` and "4 subtasks · 2 h 30 m" — read from the same field, so the
  board and the workspace cannot disagree about how far along a task is.

  **And each card carries the subtask's own checklist, tickable in place.** "No route of
  its own" has to mean *reachable from here* rather than unreachable: a subtask holds items
  exactly as a leaf task does — the first one inherits the parent's when the parent becomes
  composite ([02-data-model.md](02-data-model.md#task-items)) — so without this the coach
  could plan work the learner had no way to see. `GET /api/tasks/{id}` already returns
  `subtasks[]` with their items, so it still costs no extra request. No budget meter on
  these: the meter compares a checklist against the report that produced it, and the report
  fetched here is the *parent's*.
- **The checklist:**

```
┌ To complete this task ─────────────── 38 of 45 min ┐
│ ☑ Read §3–4 on structured concurrency      12 min  │  ← unguided: link, opens out
│ ☐ Watch "Nurseries explained"              14 min  │  ← unguided: YouTube, channel
│ ☐ Work through the cancellation exercise   12 min  │  ← guided: "with your coach"
└────────────────────────────────────────────────────┘
```

  `task.items[]`, in array order, each with a checkbox writing
  `PATCH /api/tasks/{id}/items/{itemId}`. Before M8 this sat beside an inline block
  rendering the latest report's `optional[]` and citations directly on this screen; M8
  moves that block into its own view (below), because it is exactly the content a
  tool-heavy research turn used to write into this task's own transcript, and the split
  that stopped the transcript from interleaving it does the same for the workspace's
  layout.
- **A guided item and an unguided one look different, because they ask for different
  things.** An unguided item renders its `details` — the link, the section, the video's
  channel and duration — because that is the instruction, and it carries a "Mark done"
  affordance for the learner to report back with. A guided item renders only its
  `shortDescription` and a "with your coach" marker; its `details` are the coach's teaching
  notes and showing them to the learner would hand over the answer to the exercise. **Do not
  render `item.details` for a guided item**, in this screen or any future one.
- **Each item's `kind` — article, video, exercise, doc, scaffold — is its own small icon +
  label chip, `ItemKindBadge` (`components/task/item-kind.tsx`), shown for any item that
  has one.** One module owns the icon and label for all five kinds, imported everywhere an
  item is listed — this checklist, `ResearchReport`'s required/optional lists, and a
  `ProposedTaskCard`'s expanded material — so the same report item reads identically
  whether it is still on the report, proposed in a study plan, or promoted onto a real
  checklist. `null` (a hand-added item, or one added before `kind` existed on `TaskItem` —
  [02-data-model.md](02-data-model.md#task-items)) renders no chip rather than a blank one.
  The same module's `ItemKindStrip` is the coarser, task-level version of this — an
  optional duration plus one chip grouping every item's kind by icon and count, the count
  always shown even at one ("1x[book] 2x[pen]") so the chip reads the same way regardless
  of whether something repeats. **Two groups within the one chip**, not a single merged
  count: required items (the chip's ordinary styling, a solid underline) and, introduced
  by a `+`, optional items (very slightly muted, a dotted underline instead) — a required
  item is a thing the task's size already accounts for, an optional one is a deep dive the
  learner may or may not take, and the chip tells them apart at a glance the same way the
  task's own material lists do. Used on a `ProposedTaskCard`'s info line (with duration,
  since nothing else on that card shows one, and both groups — the proposed task's
  `required[]`/`optional[]`) and a leaf `TaskCard`'s item-progress row (icons only, since
  `estimate` above it already shows the task's duration and repeating it would be the same
  number twice on one card; no optional group there either, since only `required[]` is
  ever promoted onto a real task — [02-data-model.md](02-data-model.md#task-items)).
- A running "X of Y min" budget meter over the checklist.
- **A "Latest research" card, below the checklist, added at M8.** The same component the
  board shows, fed by `GET /api/tasks/{id}/runs` instead of the project route — the most
  recent run whose steps include `research`. Brief description (the report's `summary`,
  truncated) plus creation date and status; clicking it opens the
  [research view](#research-view-projectsprojectidresearchrunid) for that run. Absent
  entirely for a task that has never been researched. Beside it, "View previous research
  (N)" reveals the task's earlier runs the same way the board's does — see below.
- "Research this task now" button → `POST /api/sessions/{sid}/research`, then navigates
  straight into the research view for the run it started — that screen is where the
  turn's progress (tool chips, streamed generation) is watched, since the turn no longer
  runs in this screen's own session. On a `draft` task this is the screen's primary
  action; once materials exist it moves into the research card's header as "Research
  again."
- **Beside it, "Have my coach prepare this"** → `POST /api/tasks/{id}/research-request`.
  The same research, queued instead of watched: the learner marks the task, closes the tab,
  and the next tick runs it headless with priority over auto-scheduled work
  ([05-autonomous-runs.md](05-autonomous-runs.md#two-kinds-of-work-and-the-only-difference-between-them)).
  Two buttons for one outcome is a real cost, and it is paid deliberately — the queued path
  is the one intended to become the *only* path once the autonomous agent has been shown to
  work unattended, and the inline button is what keeps the feature usable while that is
  being established.

  Once queued, the control becomes **"Starts soon — cancel"** →
  `DELETE /api/tasks/{id}/research-request`. The cancel affordance exists because the
  alternative is a mis-queued task that cannot be recalled until it has spent a run and a
  quota slot, and it disappears the moment `researchStatus` leaves `pending` — by then a
  run holds the task and the honest control is the turn's cancel, not this one.

  Both buttons are hidden on a composite task, on the same reasoning as the inline one: its
  subtasks are its plan, and each is researched on its own.
- **A failed run reads as an offer, not an error.** `researchStatus == "failed"` renders
  "Your coach couldn't prepare this — try again", which re-queues. The retry is a press
  rather than an automatic re-enqueue precisely so that a task the research agent cannot
  handle costs one run per decision the learner makes, instead of one per tick.

Right — **session chat**:
- Streamed markdown, rendered ([below](#markdown-in-the-transcript)); tool activity as inline status chips
  ("Searching the web…", "Checking video lengths…") built from `tool_call`/`tool_result`.

  **A chip carries a detail, not only a label.** "Adding a task" says an action happened;
  "Adding a task · Read the asyncio guide (45 min)" says which, and is the difference
  between a record the learner can audit and one that merely proves the coach was busy.
  The detail is written from the call's arguments first and its result second
  (`lib/tool-labels.ts`), because a call still running or refused has no result — and for
  `ask_learner` it is written from the result, since the interesting half of that call is
  **the learner's own answer**, which appears nowhere else in the conversation.

  **A question from the coach renders as controls.** `ask_learner` posts single- or
  multi-select options with an optional note through the same confirmation handshake that
  gates `discard_task` ([03-agent-design.md](03-agent-design.md#asking-the-learner-something)),
  and `QuestionPrompt` renders them beside the transcript rather than in a modal — the
  answer becomes part of the conversation, and a dialog that covers the conversation hides
  the context the question is about. "None of these" appears only when the tool said it was
  a real answer.

  **The roadmap brief approval dialog** — `ConfirmationPrompt` renders `propose_roadmap_brief`'s dialog from `Project.roadmapBrief`
  — the document `write_roadmap_brief` last stored (`useProject`, already in cache by the
  time this conversation exists) — not from the confirmation call's own arguments. The
  model is asked to reproduce the stored brief exactly, but that instruction is not an
  enforcement: rendering from the call's arguments would show the learner the model's
  restatement of the brief, and the point of an approval dialog is to show them the thing
  they are approving. Falls back to the call's arguments only if `roadmapBrief` has not
  loaded — defensive, not the intended path.

  **Attachments render as a checklist, not read-only chips — every file the conversation
  has, not only the ones the model already thought to reference.** `sessionAttachmentNames`
  (`lib/transcript.ts`) lists every distinct filename the user has sent in the session,
  oldest first, the frontend's own approximation of `SessionService.list_attachments`; each
  becomes one `BriefAttachmentOption` row — a `Checkbox` + `Label`, pre-checked to match
  `Project.roadmapBrief.attachments` — so the learner can tick or untick any of them right
  in the dialog and approve in one step, rather than typing a correction and waiting on
  another model turn. The checked set is tracked as its own `touchedAttachments` state,
  `null` until the learner edits it (the same "no opinion yet" pattern the description
  checkbox below uses, for the same reason: a background refetch of `project` must not
  silently discard an edit); on approval, the *edited* selection — not the brief's
  original one — rides in the confirm answer's payload
  (`CONFIRMED_ATTACHMENTS_KEY`), which `_propose_roadmap_brief`
  (`agents/tools.py`) applies deterministically, overriding whatever the model's own
  `attachments` argument said, rather than needing the model to notice the edit and
  re-propose. An image attachment additionally gets a small hover preview: `findAttachmentRef`
  (`lib/transcript.ts`) resolves the filename back to the transcript event that carries it,
  and a `Tooltip` (`components/ui/tooltip.tsx`, a thin wrapper over Base UI's, same pattern
  as `checkbox.tsx`) fetches the bytes through `api.getEventAttachment` — the same
  authenticated-fetch-to-object-URL path `AttachmentPreview` uses in the ordinary
  transcript — lazily, on the tooltip's own `onOpenChange`, so opening the dialog does not
  fetch every attachment in the conversation whether or not the learner looks at any of
  them. The checklist itself is omitted entirely when the conversation has no attachments
  at all, and in that case the confirm answer carries no override — `_confirmed_attachments`
  on the backend then falls back to the model's own argument, which is also the only
  case that argument still matters for.

  **The study plan approval dialog's description checkbox.**
  `materialize_study_plan`'s dialog adds a checkbox, "Also update project description" —
  checked by default when `Project.description` is empty
  ([03-agent-design.md](03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer)).
  Once the learner has touched it, a background refetch of `project` must not silently
  flip it back, so the default is applied once (tracked with its own "have I set this yet"
  flag) rather than derived from `project.data` on every render. The checkbox's current
  value rides in the confirm button's own answer payload
  (`UPDATE_PROJECT_DESCRIPTION_KEY`), the same "answer carries a preference" arrangement
  `complete_task_item`'s "and stop asking" button already uses.

  **Every message carries a copy control**, yielding its *source* text rather than the
  rendered output — a table pastes back as a table, and the learner's own message comes
  back exactly as typed. That second half is what makes rendering their markdown safe:
  the transcript is still the record of what they sent, one click away.

  **The chips are part of the transcript, not only of the stream.** A turn's live buffer
  is cleared on `turn_complete`, so chips rendered only from `useStreamStore` exist for
  the few seconds a turn is generating and then vanish — leaving a conversation in which
  the board changed by itself. `lib/transcript.ts` therefore rebuilds them from the
  stored events, pairing each `function_call` with its `function_response` by call id.
  Three outcomes, not two: a tick, a cross for a tool that refused, and a neutral mark
  for one whose outcome was never recorded — an interrupted turn, or a call still waiting
  on the confirmation prompt below.
- Composer with drag-and-drop upload (image/PDF/text), preview thumbnails, paste-image
  support. **The drop target is the whole chat pane, not the composer strip** — a
  two-line strip is a target people miss, and missing it is worse than having none,
  because the browser's default action for a file dropped on a page is to navigate away
  from the app.
- **The text area is its own full-width row, with a second row of controls below it**,
  added shortly after M8 — a textarea sharing a row with buttons had less room to grow and
  read as a text field with icons bolted on. That control row splits by side: attachment
  actions (the paperclip today, more expected to join it) sit left, and send/cancel sits
  right, alone — the one action that actually dispatches the message, where a reader's eye
  lands last.
- **Attachments in the transcript render as thumbnails when they are images**, and as a
  named chip otherwise. Added at M2 rather than specified here originally; a conversation
  about a screenshot reads badly when the screenshot is a word.

  The bytes come from `GET /api/sessions/{sid}/events/{seq}/attachments/{index}` through an
  authenticated `fetch`, and become an object URL. An `<img src>` pointing straight at the
  endpoint cannot work — an `<img>` sends no `Authorization` header — and giving the URL its
  own credential would add a second way into the data, against
  [00-overview.md](00-overview.md)'s one-auth-path decision. Loading is lazy, so a long
  conversation does not fetch every image in it to render the last screen.

  What the transcript can show depends on which side it is looking at: a message still in
  flight knows the user's filename, and a stored event does not unless
  `TurnService._build_content` put it in `file_data.display_name` — the artifact itself is
  named `user:{uploadId}` and the `gs://` URI has no human segment.
- **The chat pane is height-bounded and the transcript scrolls itself**, on mobile as well
  as on desktop. The transcript pins to its own bottom as tokens arrive, and it does that
  by assigning `scrollTop` rather than calling `scrollIntoView` on a sentinel:
  `scrollIntoView` scrolls every scrollable ancestor, the document included, so on a short
  viewport it moved the *page* once per delta. The composer then walked under the reader's
  finger and the cancel button was unreachable exactly while there was something to
  cancel — and anyone scrolling up to reread a message was dragged back down several times
  a second. An unbounded pane has the same effect for the same reason, since without
  overflow there is nothing else to scroll.
- A visible **reconnecting** state that makes the resume guarantee legible: "Connection
  lost — your coach is still working. Reconnecting…" then the stream continues from where
  it left off.
- Cancel button (the only thing that stops generation).

### Research view (`/projects/:projectId/research/:runId`)

Added at M8, once research runs got their own sessions
([03-agent-design.md](03-agent-design.md#the-research-pipeline-since-m9)), as a split view —
transcript on one side, results on the other. **Reworked to a single, toggled pane** once a
roadmap run gave this screen a second kind of result (a `StudyPlan`, not just a
`ResearchReport`): a full-width transcript and a full-width results view, switched by a
`ToggleGroup` rather than fought over as two half-width panes. Reached from the "Latest
research" card on the board or a task workspace, or directly after starting a run from
either screen.

A header names what is being researched — the task's title, linked back to its workspace,
or "Research for this project" when `taskId` is `null` (a research run) or "Roadmap for
this project" (a roadmap run, told apart by `steps[0].id === 'roadmap'` —
[04-api-contract.md](04-api-contract.md#post-apisessionssidroadmap)). Below it,
`ResearchInfoStrip` — the task workspace's always-visible `TaskInfoStrip`
([above](#task-workspace-projectsprojectidtaskstaskid)), adapted: the run's status and
creation date on one side, the `ToggleGroup` on the other. Its two options are "Transcript"
and a results label that names what is actually behind it — "Report" for a research run,
"Study plan (draft)" for a roadmap run — so the control itself says what a click will show
before it is clicked. **Defaults to results**: the interesting thing about a *finished* run
is what it produced, not the transcript of how it got there.

**Both views stay mounted regardless of which is showing** — the hidden one gets Tailwind's
`hidden`, not a conditional that removes it from the tree. `SessionPane` owns a socket
subscription and a stream buffer for the run's turn; unmounting it to switch views would
tear both down and force a resubscribe (and possibly lose scroll position) on switching
back, for a screen a learner may flip between more than once while a run is still
streaming. The results view costs nothing to keep mounted either way — it is just a query
result.

**Transcript** — the research session, read from `GET /api/runs/{runId}` for `sessionId`
and then rendered by the same `SessionPane` the task workspace and the board's intake
conversation use — streamed markdown, tool-activity chips, reconnect handling, all of it
unchanged. **It has no composer, and that is a deliberate difference from every other use
of `SessionPane`.** This pane is a record of what the coach did to prepare the material —
the searches, the pages it read, the video it checked the length of — not a conversation
the learner is having; no node of the research pipeline has a tool that asks the learner
anything, so a composer here would invite a message nothing is instructed to read. A
**cancel** button is still offered while the run is `running`, on the same reasoning the
task workspace's chat cancel has: it is the only thing that stops generation, and this is
generation like any other.

**Every branch interleaves chronologically.** One session now holds a branched pipeline
instead of one linear turn: `research_planner` and `reviewer_writer` (or, for a roadmap run,
`task_proposer`/`plan_tailor`) share the session's default branch, and each
`topic_researcher` gets its own sub-branch — bookkeeping only, not a content-isolation
mechanism (see [03-agent-design.md](03-agent-design.md#the-research-pipeline-since-m9)).
`transcript.ts`'s `toMessages` sorts by `seq` alone and has never read `branch`, so a
multi-branch pipeline needed no rendering change to interleave correctly, on a pane where
three to five sub-topics' searches and fetches can genuinely interleave line-for-line. A
per-`topic_researcher`-branch disclosure — grouping each sub-topic's activity behind its own
expandable section rather than interleaving it — would be a new grouping concept this
project has never needed; left for later if plain interleaving reads as noise once there is
a real multi-sub-topic transcript to look at.

**Since the roadmap pipeline, every model message shows its `author`.** `author` was always
carried on `TranscriptMessage` (`lib/transcript.ts`, tested since M9) but never rendered —
every model message read as an anonymous "the coach" bubble, coach included. Now that a
research or roadmap session's transcript can genuinely hold several named agents
(`research_planner`, `topic_researcher`, and then `reviewer_writer` *or*
`task_proposer`/`plan_tailor`), `Transcript.tsx` renders a small muted label above each model
message with its `author` — not shown for the learner's own messages, since `author` there
is always literally `"user"` and the bubble's alignment already says so.

**A debug toggle, `SessionPane`'s one non-conversational control, shows each message's raw
ADK event id next to its author.** `useDebugUiStore` (`stores/debugUi.ts`) is a single
persisted boolean, flipped by a small ghost button in the pane's header — global rather than
per-session, because comparing event ids is a thing done *across* a transcript. Off by
default: an ordinary coach conversation has one author for its whole transcript and nothing
interesting to compare ids against, so the button earns almost all of its value on a
research or roadmap pane.

**Results** — **the final report**, once there is one — or, for a roadmap run, the
`StudyPlan` `plan_tailor` wrote instead. States, read from `GET /api/runs/{runId}` and,
once it exists, `GET /api/runs/{runId}/report` / `GET /api/runs/{runId}/plan`:

- **Running** — "Your coach is researching…" (or "…is building a roadmap…" for a roadmap
  run), no report yet. The pane does not poll for one; `post_research_report`/
  `write_study_plan` pushes `board_update`, which is what flips this state the moment the
  report exists, same as the task workspace's checklist appearing today.
- **Complete, roadmap run** — `StudyPlanView` (`components/research/`), fetched from
  `GET /api/runs/{runId}/plan` rather than `.../report`: `GET /api/runs/{runId}/report`
  reads `research_reports`, and a roadmap run wrote to `study_plans` instead
  ([03-agent-design.md](03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer)).
  The plan's `title`/`shortDescription`/`longDescription` (markdown) sit above a list of
  `ProposedTaskCard`s, one per proposed task, ordered as `plan_tailor` wrote its `plan[]`.
  Every card always shows two things without a click: a corner chip naming
  `plan_tailor`'s decision (`include` → "Included", `additional` → "Deep dive",
  `exclude` → "Not included", `reject` → "Rejected") and the one-line `why` beneath it —
  the point of an excluded or rejected entry is the reason, and hiding it behind the same
  disclosure as the material would bury the one thing a learner scanning the list actually
  wants from a task that didn't make the cut. A card for a task `plan_tailor` did *not*
  select (`exclude`/`reject`) renders at reduced opacity, so the plan reads at a glance as
  "what's in" before "what was considered"; the required/optional material itself —
  same item shape `ResearchReport` renders, each item's kind chip included
  ([above](#task-workspace-projectsprojectidtaskstaskid)) — is behind a click either way,
  since a plan with several proposed tasks would otherwise be as long as the report it
  replaces. The plan is read-only and immutable here, same as everywhere else it appears
  ([03-agent-design.md](03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer)) —
  no control on this screen selects, reorders, or materializes a task. Below the task
  list, `task_proposer`'s own `memo` — carried through `plan_tailor`'s `write_study_plan`
  call unchanged — sits behind a "Task composer's memo" disclosure (chevron, closed by
  default) and renders as markdown once opened: it is `task_proposer`'s own note on how it
  grouped the research, addressed to whoever reads the plan next rather than to the
  learner directly, so it earns a click rather than a place in the always-visible summary.
- **Failed** — the run's `error`, and a "Try again" action that starts a fresh run (a new
  `runId`, a new session) rather than retrying this one in place — consistent with a
  failed run being terminal everywhere else in the ledger.
- **Complete** — the report: `summary` rendered as markdown
  ([below](#markdown-in-the-transcript)) — the full write-up M8 asks the model for, not
  the two-or-three-sentence blurb it used to be — then `required[]` and `optional[]` (for
  a task-scoped report; a project-scoped one has both but nothing was promoted anywhere,
  each item carrying its kind chip — [above](#task-workspace-projectsprojectidtaskstaskid)),
  then citations from grounding metadata. This is exactly the content `ResearchReport.tsx`
  rendered inline in the task workspace before M8; M8 relocates the component rather than
  rewriting it. Item feedback (thumbs up/down) stays available on a task-scoped report's
  `optional[]` items, reached the same way it always was —
  `PATCH /api/reports/{reportId}/items/{itemId}`. **A project-scoped report renders
  without the feedback control** — that endpoint checks ownership through the task
  (`ReportItemFeedback.taskId`), which a `taskId: null` report has none of, and adding a
  second ownership path for a thumbs-up on further reading is not worth doing until
  project-scoped research is more than a first cut.

A run with `taskId: null` renders identically, minus anything that would name a task: the
header reads "Research for this project" with no link back to a task, and the
required/optional lists read as a plan for the question the learner asked rather than for
a checklist nothing here promotes into.

### Settings — "What your coach knows about you"

Renders `learnerProfile` as editable fields with the agent's stated evidence and the
`version`/`updatedAt` of each change. Per-field reset and a global "start fresh." This
turns the evolving user model from an opaque behaviour into a product feature, and is the
first thing to look at when the coach starts behaving strangely.

**Skills, not "Technologies"**, since a learner may use the coach for a subject that is
not a technology at all (math, writing, a language). Each entry is a `name` plus the
`area` it was observed in — a subject or technology, "general" for one that is not tied to
a single subject — alongside `level` and `evidence`; `LearnerProfileEditor`'s add-skill
form has its own input for `area`, and the read-only list shows it as a chip beside the
name so a learner scanning the list can tell "type hints (Python)" from "type hints
(TypeScript)" at a glance. `render_learner` (`agents/prompt.py`) renders the same pairing
into the coach's own prompt, for the reason `SkillBelief.area`'s own docstring gives: a
belief formed studying one subject says nothing about the learner's standing in another.

## Markdown in the transcript

Both session panes — the task workspace's and the board's intake conversation — render the
**coach's** messages as markdown rather than as preformatted text. The coach answers with
study plans in tables, equations, code, and diagrams; showing the reader `| --- |` and
`$\int$` is showing them the notation instead of the thing.

**The learner's own messages stay preformatted, and that is deliberate.** Their message is
the record of what they sent: rendering it as markdown would collapse the line breaks they
typed, reflow a pasted stack trace into a paragraph, and turn a literal `*` into emphasis.
The coach writes markdown knowingly; a person typing into a chat box does not.

The stack is assembled from one plugin per capability rather than adopted whole, so that
each piece can be reasoned about, pinned, and swapped alone:

| Capability | What renders it |
| --- | --- |
| The document | `react-markdown` — no `rehype-raw`, see below |
| Tables, task lists, strikethrough, autolinks | `remark-gfm` |
| Equations, `$…$` and `$$…$$` | `remark-math` → `rehype-katex`, with KaTeX's stylesheet imported once |
| Fenced code | `shiki`, dynamically imported |
| ` ```mermaid ` fences | `mermaid`, dynamically imported |

**Raw HTML stays off.** react-markdown drops embedded HTML unless `rehype-raw` is added,
and it is deliberately not added. This transcript renders text a language model produced,
some of it quoted from pages the coach fetched during research — the one thing that must
not be reachable from there is markup in our DOM. The single exception is the SVG mermaid
itself returns, and mermaid runs at its default `securityLevel: 'strict'`, which sanitizes
its own output.

**Markdown renders while the turn is streaming; mermaid waits for the end of it.** Half a
table is still a table and half a heading is still a heading, so a live re-parse per delta
is worth it — but half a graph is a syntax error, and a diagram that flashes an error box
for the two seconds its definition is arriving is worse than one that appears a moment
late. A ` ```mermaid ` fence therefore renders as a code block for as long as the turn is
generating and becomes a diagram once `turn_complete` hands the text to the transcript.
That also keeps mermaid's several hundred kilobytes off the streaming path entirely.

**Syntax highlighting is dual-theme, not two stylesheets.** Shiki is asked for
`github-light` and `github-dark` in one call with `defaultColor: false`, so every token
span carries both `--shiki-light` and `--shiki-dark`, and a rule gated on the root `.dark`
class picks between them. The theme switch stays what it is everywhere else — one class on
`<html>` — nothing re-highlights, and a code block from ten minutes ago flips with the rest
of the page. This is the concrete form of the requirement in
[Integration points](#integration-points-that-are-easy-to-miss).

Mermaid has no equivalent trick, because its output is baked SVG. A diagram re-renders when
`resolved` changes, which is rare enough to be uninteresting.

**Both lazy loads fail soft.** While shiki's chunk is in flight — and permanently, if it
never arrives — a code block is a plain `<pre><code>` containing the code, which is
perfectly readable; mermaid failing to load or failing to parse leaves the diagram's
source on screen rather than an error. A coach whose rendering library 404s should still
be legible, and neither failure may take the transcript down with it.

**Cost control is a cache and a memo, not a debounce.** Re-parsing the whole live message
on every delta is cheap for the sizes involved, but re-highlighting is not, so a
highlighted block is memoized on `(code, language)` and the highlighter itself is a single
lazily created promise shared by every block on the page. Nothing here belongs in the
Query cache; see [the state split](#state-management-split).

## Theme (light, dark, system)

Two distinct values, and conflating them is the usual source of bugs:

- **`pref`** — what the user chose: `light | dark | system`. Persisted.
- **`resolved`** — what is actually painted: `light | dark`. Derived; never stored as the
  user's choice.

`system` is a real, persistent state, not the absence of a choice — so the control is a
three-way segmented control, never a binary toggle.

**Theme lives in `localStorage`, not in `globalPrefs`.** It has to be readable before React
mounts and before auth resolves — the login screen needs a theme too — and an authenticated
round-trip cannot meet that. Device-specific is also usually what people want. Cross-device
sync stays possible later by seeding `localStorage` from a server value after sign-in, but
`localStorage` remains the paint-time source of truth.

### Applied before first paint

Tailwind's `class` strategy plus shadcn CSS variables means the theme is one class on
`<html>`. There is no SSR, so without intervention the page paints light, mounts React, then
flips — a visible flash that is worst in dark mode. A small **blocking, inline** script in
`index.html`, before any stylesheet or module, prevents it:

```html
<script>
  (function () {
    try {
      var pref = localStorage.getItem('coach.theme') || 'system';
      var dark = pref === 'dark' ||
        (pref === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
      document.documentElement.classList.toggle('dark', dark);
      document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    } catch (e) { /* private mode: fall through to light */ }
  })();
</script>
```

It must be inline and non-`defer`/non-`module`, or it runs too late to help. Author it in
`apps/web/index.html`; Vite emits it into `dist/index.html`, which the image copies to
`static/index.html` ([07-infra-deploy.md](07-infra-deploy.md#container)). Keep it inline
rather than importing a module — an external file would be another round trip before paint,
which is the thing being avoided.

**Storage-key coupling.** This script and `useThemeStore` must agree on the key *and the
value format*. Zustand's `persist` middleware wraps state in a `{state, version}` envelope,
which a naive inline reader would choke on. So `pref` is written to a plain
`localStorage['coach.theme']` string by a store subscriber rather than through `persist`.
This coupling is deliberate and fragile — a test asserts the two stay in sync.

**`color-scheme` is set alongside the class.** Without it, native scrollbars, form controls,
and the pre-paint canvas stay light while everything else goes dark.

### Reacting to the system

When `pref === 'system'`, a `matchMedia('(prefers-color-scheme: dark)')` `change` listener
re-resolves live, so the app follows the OS switching at sunset without a reload. The
listener is attached always but is a no-op unless `pref` is `system` — simpler than
attaching and detaching on every preference change.

### The control

On `/settings`, under **Appearance**: a shadcn `ToggleGroup` with Radix `radiogroup`
semantics, three options, arrow-key navigable. When `System` is selected it shows what that
currently means — "System — currently dark" — so the resolved state is never a mystery.

The theme switch is **not animated**. A full-page color transition is unpleasant and is a
motion-sensitivity trigger; if one is ever added it must be gated behind
`prefers-reduced-motion`.

### Integration points that are easy to miss

- **Syntax highlighting** in the transcript needs its own light/dark swap — the highlighter's
  palette is not covered by shadcn variables. Drive it from CSS variables gated on the root
  `.dark` class rather than swapping stylesheets at runtime. Shiki's dual-theme output is
  exactly that shape ([Markdown in the transcript](#markdown-in-the-transcript)).
- **Mermaid and KaTeX are two more palettes outside shadcn's.** Mermaid takes a theme at
  render time, so it is the one thing on the page that must actually re-render when
  `resolved` changes; KaTeX inherits `currentColor` and needs nothing, which is worth
  knowing so nobody goes looking for a switch that should not exist.
- **The research report's required/optional blocks** rely on "different container styling" to
  encode a product requirement. That distinction must survive both themes and must not be
  carried by color alone — it already leans on headings and the presence of checkboxes,
  which is what makes it safe.
- **Progress rings and the budget meter** need tokens for both themes, not hard-coded hex.
- **`<meta name="theme-color">`** is updated on change so mobile browser chrome matches.
- **Focus rings** must clear contrast in both themes; they are the easiest thing to lose when
  a dark palette is tuned for looks.

## Accessibility & polish

- All shadcn primitives keep their Radix semantics; drag-and-drop has a keyboard fallback
  (move up/down in the row action menu).
- Streaming text uses `aria-live="polite"` on a debounced container, not per-token, to
  avoid screen-reader spam.
- Duration formatting is one shared `formatMinutes()` (`45 min`, `1 h 30 m`) used by cards,
  rollups, and the budget meter.
- Light and dark are equal-status themes, not a default plus a variant: contrast, focus
  rings, and the required/optional report distinction are audited in both
  (see [Theme](#theme-light-dark-system)).
