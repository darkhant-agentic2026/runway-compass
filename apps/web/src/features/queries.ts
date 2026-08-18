/**
 * TanStack Query wiring: keys, the client, and the mutations that must feel instant.
 *
 * docs/06-frontend.md fixes the split — Query owns anything the server can also change —
 * and the key shapes below are the table from that document.
 */

import {
  QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query'

import { api, ApiError, newIdempotencyKey } from '@/lib/api'
import { orderForMove } from '@/lib/ordering'
import type {
  GlobalPrefs,
  Project,
  ProjectPrefs,
  SessionEvent,
  Task,
  TaskState,
  TaskWithSubtasks,
} from '@/lib/schemas'
import { getSocket } from '@/lib/socket'
import type { BoardFilters } from '@/stores/boardUi'
import { useStreamStore } from '@/stores/stream'

export const queryKeys = {
  me: ['me'] as const,
  projects: ['projects'] as const,
  project: (projectId: string) => ['project', projectId] as const,
  effectivePrefs: (projectId: string) => ['project', projectId, 'effective-prefs'] as const,
  tasks: (projectId: string, filters: BoardFilters) => ['tasks', projectId, filters] as const,
  task: (taskId: string) => ['task', taskId] as const,
  taskSession: (taskId: string) => ['task', taskId, 'session'] as const,
  /**
   * Deliberately *not* under the `['project', id]` prefix. `board_update` and every
   * project mutation invalidate that prefix, and this key is a get-or-create POST — an
   * invalidation would re-issue it on every board change for a value that never moves.
   */
  projectSession: (projectId: string) => ['project-session', projectId] as const,
  sessionEvents: (sessionId: string) => ['session', sessionId, 'events'] as const,
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The WebSocket tells us when board data is actually stale (from M2), so
        // aggressive refetch-on-focus is unnecessary noise.
        staleTime: 30_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          // Retry with backoff except on 4xx: a 404 or a 422 will not improve.
          if (error instanceof ApiError && error.isClientError) return false
          return failureCount < 3
        },
      },
      mutations: { retry: false },
    },
  })
}

// --- queries --------------------------------------------------------------------------

export function useMe() {
  return useQuery({ queryKey: queryKeys.me, queryFn: api.getMe })
}

export function useProjects(status?: Project['status']) {
  return useQuery({
    queryKey: status ? [...queryKeys.projects, status] : queryKeys.projects,
    queryFn: () => api.listProjects(status),
  })
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: queryKeys.project(projectId),
    queryFn: () => api.getProject(projectId),
  })
}

export function useEffectivePrefs(projectId: string) {
  return useQuery({
    queryKey: queryKeys.effectivePrefs(projectId),
    queryFn: () => api.getEffectivePrefs(projectId),
  })
}

export function useBoard(projectId: string, filters: BoardFilters) {
  return useQuery({
    queryKey: queryKeys.tasks(projectId, filters),
    queryFn: () =>
      api.listTasks(projectId, {
        includeCompleted: !filters.hideCompleted,
        includeDiscarded: !filters.hideDiscarded,
        includePostponed: !filters.hidePostponed,
      }),
  })
}

// --- mutations ------------------------------------------------------------------------

/** Apply `patch` to one task wherever it sits in the nested board. */
function patchBoardTask(
  board: TaskWithSubtasks[],
  taskId: string,
  patch: Partial<Task>,
): TaskWithSubtasks[] {
  return board.map((parent) => {
    if (parent.id === taskId) return { ...parent, ...patch }
    if (!parent.subtasks.some((child) => child.id === taskId)) return parent
    return {
      ...parent,
      subtasks: parent.subtasks.map((child) =>
        child.id === taskId ? { ...child, ...patch } : child,
      ),
    }
  })
}

interface BoardMutationContext {
  previous: TaskWithSubtasks[] | undefined
}

/**
 * The `onMutate` → snapshot → patch → `onError` rollback → `onSettled` invalidate shape
 * that docs/06-frontend.md prescribes for the interactions that must feel instant.
 */
function useOptimisticBoardMutation<TVariables>(
  projectId: string,
  filters: BoardFilters,
  mutationFn: (variables: TVariables) => Promise<unknown>,
  optimisticPatch: (board: TaskWithSubtasks[], variables: TVariables) => TaskWithSubtasks[],
): UseMutationResult<unknown, Error, TVariables, BoardMutationContext> {
  const queryClient = useQueryClient()
  const key = queryKeys.tasks(projectId, filters)

  return useMutation<unknown, Error, TVariables, BoardMutationContext>({
    mutationFn,
    async onMutate(variables) {
      await queryClient.cancelQueries({ queryKey: key })
      const previous = queryClient.getQueryData<TaskWithSubtasks[]>(key)
      if (previous) {
        queryClient.setQueryData<TaskWithSubtasks[]>(key, optimisticPatch(previous, variables))
      }
      return { previous }
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous)
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}

export function useSetTaskState(projectId: string, filters: BoardFilters) {
  return useOptimisticBoardMutation<{
    taskId: string
    state: TaskState
    postponedUntil?: string | null
  }>(
    projectId,
    filters,
    ({ taskId, state, postponedUntil }) => api.setTaskState(taskId, state, postponedUntil),
    (board, { taskId, state, postponedUntil }) => {
      const patched = patchBoardTask(board, taskId, {
        state,
        postponedUntil: postponedUntil ?? null,
      })
      // Promoting a task to `current` demotes whatever was current — mirror the
      // server's invariant locally, or the board briefly shows two "Next up" rows.
      if (state !== 'current') return patched
      return patched.map((parent) => ({
        ...parent,
        state:
          parent.id !== taskId && parent.state === 'current' ? 'not_started' : parent.state,
        subtasks: parent.subtasks.map((child) => ({
          ...child,
          state: child.id !== taskId && child.state === 'current' ? 'not_started' : child.state,
        })),
      }))
    },
  )
}

/**
 * A subtask's state, changed from inside the parent's workspace.
 *
 * The board's `useSetTaskState` patches `['tasks', projectId, filters]`, and the workspace
 * does not read that key at all — it reads `['task', parentId]`. Reusing the board's hook
 * here would fire the request and leave the rows on screen unchanged until a refetch
 * landed, which for a click on "Complete" is exactly the latency optimistic updates exist
 * to remove. So this is the same `onMutate` → snapshot → patch → rollback → invalidate
 * shape against the detail entry.
 *
 * **The invalidation is `exact`.** `queryKeys.taskSession` is `['task', id, 'session']`,
 * a get-or-create POST under the same prefix, and a prefix invalidation would re-issue it
 * on every subtask click.
 *
 * `rollup` is deliberately *not* recomputed optimistically: it is the server's number
 * (`services/rollups.py`), and a second implementation of that arithmetic in TypeScript is
 * a thing to keep in step for the sake of one round trip's worth of ring animation. The
 * row updates instantly; the ring updates when the invalidation lands.
 */
export function useSetSubtaskState(parentTaskId: string, projectId: string) {
  const queryClient = useQueryClient()
  const key = queryKeys.task(parentTaskId)

  return useMutation<
    unknown,
    Error,
    { taskId: string; state: TaskState; postponedUntil?: string | null },
    { previous: TaskWithSubtasks | undefined }
  >({
    mutationFn: ({ taskId, state, postponedUntil }) =>
      api.setTaskState(taskId, state, postponedUntil),
    async onMutate({ taskId, state, postponedUntil }) {
      await queryClient.cancelQueries({ queryKey: key, exact: true })
      const previous = queryClient.getQueryData<TaskWithSubtasks>(key)
      if (previous) {
        queryClient.setQueryData<TaskWithSubtasks>(key, {
          ...previous,
          subtasks: previous.subtasks.map((child) =>
            child.id === taskId
              ? { ...child, state, postponedUntil: postponedUntil ?? null }
              : // Promoting one subtask to `current` demotes whatever was current, the
                // same invariant the board mirrors.
                state === 'current' && child.state === 'current'
                ? { ...child, state: 'not_started' as const }
                : child,
          ),
        })
      }
      return { previous }
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous)
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: key, exact: true })
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}

export function useReorderTask(projectId: string, filters: BoardFilters) {
  return useOptimisticBoardMutation<{
    taskId: string
    targetIndex: number
    afterTaskId?: string
    beforeTaskId?: string
  }>(
    projectId,
    filters,
    ({ taskId, afterTaskId, beforeTaskId }) =>
      api.reorderTask(taskId, {
        ...(afterTaskId ? { afterTaskId } : {}),
        ...(beforeTaskId ? { beforeTaskId } : {}),
      }),
    (board, { taskId, targetIndex }) => {
      // The same midpoint algorithm the server runs, so the optimistic order equals the
      // confirmed order and the row does not jump when the response lands.
      const order = orderForMove(board, taskId, targetIndex)
      return patchBoardTask(board, taskId, { order }).sort((a, b) =>
        a.order < b.order ? -1 : a.order > b.order ? 1 : 0,
      )
    },
  )
}

export function useCreateTask(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { title: string; estimatedMinutes: number; afterTaskId?: string }) =>
      api.createTask(projectId, body, newIdempotencyKey()),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}

export function useSplitTask(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      taskId,
      subtasks,
    }: {
      taskId: string
      subtasks: { title: string; estimatedMinutes: number }[]
    }) => api.splitTask(taskId, subtasks),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] })
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}

export function useCreateProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { title: string; goal?: string }) =>
      api.createProject(body, newIdempotencyKey()),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects })
    },
  })
}

export function usePatchProject(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (patch: {
      title?: string
      goal?: string
      status?: Project['status']
      prefs?: Partial<ProjectPrefs>
    }) => api.patchProject(projectId, patch),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.effectivePrefs(projectId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects })
    },
  })
}

// --- sessions & turns -------------------------------------------------------------------

export function useTaskSession(taskId: string) {
  return useQuery({
    queryKey: queryKeys.taskSession(taskId),
    queryFn: () => api.openTaskSession(taskId),
    // The session is created once and never changes, so re-opening the workspace should
    // not re-POST. `staleTime: Infinity` is the honest expression of that.
    staleTime: Infinity,
    enabled: taskId.length > 0,
  })
}

/**
 * The project's intake conversation.
 *
 * `staleTime: Infinity` for the same reason `useTaskSession` uses it: the session is
 * created once and never changes, so revisiting the board must not re-POST.
 */
export function useProjectSession(projectId: string) {
  return useQuery({
    queryKey: queryKeys.projectSession(projectId),
    queryFn: () => api.openProjectSession(projectId),
    staleTime: Infinity,
    enabled: projectId.length > 0,
  })
}

export function useTask(taskId: string) {
  return useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => api.getTask(taskId),
    enabled: taskId.length > 0,
  })
}

/**
 * The transcript.
 *
 * Paged by `seq`, following `nextAfterSeq` until the server says there is no more —
 * which is also the shape `turn_complete` reuses, since a finished turn's finalized
 * events are fetched rather than assembled from the stream (docs/06-frontend.md).
 */
export function useSessionEvents(sessionId: string) {
  return useQuery({
    queryKey: queryKeys.sessionEvents(sessionId),
    enabled: sessionId.length > 0,
    queryFn: async () => {
      const collected: SessionEvent[] = []
      let cursor = 0
      for (;;) {
        const page = await api.listSessionEvents(sessionId, cursor, 100)
        collected.push(...page.events)
        if (!page.hasMore || page.nextAfterSeq === cursor) break
        cursor = page.nextAfterSeq
      }
      return collected
    },
  })
}

export interface StartTurnBody {
  text: string
  /** `filename` is used only by the optimistic echo; it is not sent to the server. */
  attachments?: { uploadId: string; mimeType: string; filename?: string }[]
  /** The answer to a tool that asked first. A turn may carry this and nothing else. */
  confirmation?: { functionCallId: string; confirmed: boolean }
}

/**
 * The synthetic event that stands in for a message the server has not stored yet.
 *
 * `seq` is one past the highest known, so it sorts last; the id is marked `pending:` so
 * it cannot collide with an ADK event id.
 */
export function pendingUserEvent(existing: SessionEvent[], body: StartTurnBody): SessionEvent {
  const parts: Record<string, unknown>[] = []
  if (body.text) parts.push({ text: body.text })
  for (const attachment of body.attachments ?? []) {
    // `snake_case`, matching what `append_event` stores, so this echo and the event that
    // replaces it are read by the same code path rather than two
    // (`session-event-vectors.json` pins the stored shape).
    parts.push({
      file_data: {
        mime_type: attachment.mimeType,
        file_uri: '',
        display_name: attachment.filename,
      },
    })
  }
  const highest = existing.reduce((max, event) => Math.max(max, event.seq), 0)
  return {
    seq: highest + 1,
    eventId: `pending:${crypto.randomUUID()}`,
    event: { author: 'user', content: { role: 'user', parts } },
  }
}

/**
 * Start a turn, echoing the user's message into the transcript immediately.
 *
 * Without the echo the message is invisible until the turn *completes*: ADK writes the
 * user event during generation and the transcript is only refetched on `turn_complete`,
 * so the sender watches "Your coach is thinking…" with no record of what they asked.
 *
 * An optimistic cache patch rather than a Zustand buffer, which is the shape
 * docs/06-frontend.md prescribes for interactions that must feel instant — and here it
 * also avoids a duplicate: the refetch on `turn_complete` replaces the whole array, so
 * the synthetic event is swapped for the stored one in a single render. A parallel copy
 * in the stream store would have to be torn down in a second step, and the frame in
 * between would show the message twice.
 */
export function useStartTurn(sessionId: string) {
  const queryClient = useQueryClient()
  const key = queryKeys.sessionEvents(sessionId)

  return useMutation<
    Awaited<ReturnType<typeof api.startTurn>>,
    Error,
    StartTurnBody,
    { previous: SessionEvent[] | undefined }
  >({
    mutationFn: (body) => api.startTurn(sessionId, body, newIdempotencyKey()),
    onMutate(body) {
      const previous = queryClient.getQueryData<SessionEvent[]>(key)
      queryClient.setQueryData<SessionEvent[]>(key, (current) => [
        ...(current ?? []),
        pendingUserEvent(current ?? [], body),
      ])
      return { previous }
    },
    onError(_error, _body, context) {
      // The message was never accepted, so it must not sit in the transcript looking as
      // though it had been.
      if (context?.previous) queryClient.setQueryData(key, context.previous)
    },
    onSuccess(turn) {
      // Register the turn before any frame arrives, so a socket that reconnects in the
      // gap between the 202 and the first delta still has something to resume.
      useStreamStore.getState().begin(turn.turnId, turn.sessionId)
      getSocket().subscribe(turn.turnId)
    },
  })
}

export function useCancelTurn(sessionId: string) {
  return useMutation({
    mutationFn: (turnId: string) => api.cancelTurn(sessionId, turnId),
  })
}

export function usePatchGlobalPrefs() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (patch: Partial<GlobalPrefs>) => api.patchPrefs(patch),
    onSuccess(me) {
      queryClient.setQueryData(queryKeys.me, me)
      // Every project's effective prefs may have moved with the global layer.
      void queryClient.invalidateQueries({ queryKey: ['project'] })
    },
  })
}
