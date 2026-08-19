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
| `/projects/:projectId/tasks/:taskId` | **Task workspace** — split view: task detail + research report on the left, session chat on the right |
| `/projects/:projectId/settings` | Project preferences (task duration, research depth, videos) |
| `/settings` | Global prefs, appearance ([theme](#theme-light-dark-system)) + **"What your coach knows about you"** (learner profile, editable) |

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
| `useStreamStore` | Per-`turnId`: accumulated text, `lastSeq`, tool-call chips, status. The hot path. |
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
  the two are the same idea and never both apply.
- **A `draft` card reads as "no plan yet", not as blocked.** The badge is muted and the card
  offers "Research this task" alongside the ordinary Start action, because `draft` is a
  state the learner may work straight out of ([02-data-model.md](02-data-model.md#task-state-machine)).
- **Parent cards show `rollup.subtaskCount` and `rollup.totalEstimatedMinutes`** ("4
  subtasks · 2 h 30 m") with a progress ring for `completedSubtasks`. Expanding reveals
  subtasks inline.
- Filters: `Hide completed` (default **on**), `Hide discarded` (default on), `Hide
  postponed` (default off). Persisted per project in `useBoardUiStore`.
- Drag-and-drop reordering (dnd-kit), optimistic, disabled while an autonomous run holds
  the project lease (the UI shows "Your coach is working on this project…" from
  `run_status`).
- Row actions: start, complete, postpone, postpone until… (date picker), discard. **Not
  split** — `POST /api/tasks/{id}/split` and its "Split into subtasks…" action were removed
  after M4. A subtask is created one at a time now, by the coach through `add_subtask` or by
  hand through `POST /api/projects/{id}/tasks` with a `parentTaskId`.

### Task workspace (`/projects/:projectId/tasks/:taskId`)

Two panes, stacked on mobile.

Left — **task detail**:
- Title, description, estimate, state control.
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
- **The checklist and the report, as two clearly separated blocks:**

```
┌ To complete this task ─────────────── 38 of 45 min ┐
│ ☑ Read §3–4 on structured concurrency      12 min  │  ← unguided: link, opens out
│ ☐ Watch "Nurseries explained"              14 min  │  ← unguided: YouTube, channel
│ ☐ Work through the cancellation exercise   12 min  │  ← guided: "with your coach"
└────────────────────────────────────────────────────┘
┌ Optional, if you want to go deeper ────────────────┐
│ ▸ Article · 30 min · …                             │
└────────────────────────────────────────────────────┘
```

  The top block is `task.items[]` — the checklist, in array order, each with a checkbox
  writing `PATCH /api/tasks/{id}/items/{itemId}`. The bottom is the latest report's
  `optional[]`, which has **no checkboxes at all**: the affordance itself encodes the
  distinction, and it is now structural rather than conventional, since the two blocks read
  from different documents.
- **A guided item and an unguided one look different, because they ask for different
  things.** An unguided item renders its `details` — the link, the section, the video's
  channel and duration — because that is the instruction, and it carries a "Mark done"
  affordance for the learner to report back with. A guided item renders only its
  `shortDescription` and a "with your coach" marker; its `details` are the coach's teaching
  notes and showing them to the learner would hand over the answer to the exercise. **Do not
  render `item.details` for a guided item**, in this screen or any future one.
- Different container styling, different heading, and a running "X of Y min" budget meter
  over the checklist.
- Citations list from grounding metadata, on the report block.
- **Report history is collapsed.** Q4 ([10-risks.md](10-risks.md#open-questions)) settles
  reports as accumulating: the newest renders expanded, older ones as a "3 earlier runs"
  disclosure. The checklist is not versioned this way — there is one, and it is current.
- "Research this task now" button → `POST /api/sessions/{sid}/research`, with progress from
  the turn's tool chips. On a `draft` task this is the screen's primary action; once
  materials exist it moves into the report block's header as "Research again."

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

### Settings — "What your coach knows about you"

Renders `learnerProfile` as editable fields with the agent's stated evidence and the
`version`/`updatedAt` of each change. Per-field reset and a global "start fresh." This
turns the evolving user model from an opaque behaviour into a product feature, and is the
first thing to look at when the coach starts behaving strangely.

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
