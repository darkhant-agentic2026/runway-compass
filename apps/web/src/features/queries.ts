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

import { ApiError, api, newIdempotencyKey } from '@/lib/api'
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
  tasks: (projectId: string, filters: BoardFilters) =>
    ['tasks', projectId, filters] as const,
  task: (taskId: string) => ['task', taskId] as const,
  taskSession: (taskId: string) => ['task', taskId, 'session'] as const,
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
          state:
            child.id !== taskId && child.state === 'current' ? 'not_started' : child.state,
        })),
      }))
    },
  )
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

export function useStartTurn(sessionId: string) {
  return useMutation({
    mutationFn: (body: {
      text: string
      attachments?: { uploadId: string; mimeType: string }[]
    }) => api.startTurn(sessionId, body, newIdempotencyKey()),
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
