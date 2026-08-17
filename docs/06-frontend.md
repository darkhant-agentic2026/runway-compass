# Frontend

Vite + React 19 + TypeScript, React Router (client-side only, no SSR), Tailwind CSS +
shadcn/ui, TanStack Query + Zustand, Vitest + Playwright.

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

- Ordered list of top-level tasks. The `current` task is visually pinned as "Next up."
- Card shows: title, estimated duration chip, state badge, research status (a small
  "materials ready" indicator), and `origin: agent` badge when the coach created it.
- **Parent cards show `rollup.subtaskCount` and `rollup.totalEstimatedMinutes`** ("4
  subtasks · 2 h 30 m") with a progress ring for `completedSubtasks`. Expanding reveals
  subtasks inline.
- Filters: `Hide completed` (default **on**), `Hide discarded` (default on), `Hide
  postponed` (default off). Persisted per project in `useBoardUiStore`.
- Drag-and-drop reordering (dnd-kit), optimistic, disabled while an autonomous run holds
  the project lease (the UI shows "Your coach is working on this project…" from
  `run_status`).
- Row actions: start, complete, postpone, postpone until… (date picker), discard, split.

### Task workspace (`/projects/:projectId/tasks/:taskId`)

Two panes, stacked on mobile.

Left — **task detail**:
- Title, description, estimate, state control.
- **Research report** rendered as two clearly separated blocks:

```
┌ To complete this task ─────────────── 38 of 45 min ┐
│ ▸ Article · 12 min · "Structured concurrency in…"  │
│ ▸ Video   · 14 min · YouTube — channel, duration   │
│ ▸ Exercise · 12 min · written by your coach        │
└────────────────────────────────────────────────────┘
┌ Optional, if you want to go deeper ────────────────┐
│ ▸ Article · 30 min · …                             │
└────────────────────────────────────────────────────┘
```

  Different container styling, different heading, a running "X of Y min" budget meter on
  the required block, and per-item checkboxes that feed completion. Optional items have no
  checkboxes at all — the affordance itself encodes the distinction.
- Citations list from grounding metadata.
- "Research this task now" button → `POST /api/sessions/{sid}/research`, with progress
  from `run_status` frames.

Right — **session chat**:
- Streamed markdown with syntax highlighting; tool activity as inline status chips
  ("Searching the web…", "Checking video lengths…") built from `tool_call`/`tool_result`.

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
- A visible **reconnecting** state that makes the resume guarantee legible: "Connection
  lost — your coach is still working. Reconnecting…" then the stream continues from where
  it left off.
- Cancel button (the only thing that stops generation).

### Settings — "What your coach knows about you"

Renders `learnerProfile` as editable fields with the agent's stated evidence and the
`version`/`updatedAt` of each change. Per-field reset and a global "start fresh." This
turns the evolving user model from an opaque behaviour into a product feature, and is the
first thing to look at when the coach starts behaving strangely.

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
  `.dark` class rather than swapping stylesheets at runtime.
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
