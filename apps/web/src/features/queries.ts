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
} from '@tanstack/react-query';

import { api, ApiError, newIdempotencyKey } from '@/lib/api';
import { orderForMove } from '@/lib/ordering';
import type {
  GlobalPrefs,
  Me,
  Project,
  ProjectPrefs,
  SessionEvent,
  Task,
  TaskDetail,
  TaskMutation,
  TaskState,
  TaskWithSubtasks,
} from '@/lib/schemas';
import { getSocket } from '@/lib/socket';
import type { BoardFilters } from '@/stores/boardUi';
import { useStreamStore } from '@/stores/stream';

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
  /**
   * Recent autonomous runs, behind the "Updated by your coach" banner.
   *
   * *Under* the `['project', id]` prefix, unlike `projectSession`, and that is the whole
   * reason the banner appears without a reload: `board_update` invalidates that prefix, so
   * the push a run sends when it changes the board also refreshes the list of what it did.
   * Keyed the other way, the banner would only ever be seen by someone who reloaded.
   */
  projectRuns: (projectId: string) => ['project', projectId, 'runs'] as const,
  /** + M8. Under `['task', id]`, on the same reasoning `reports` is: a research run
   * changes this list, and the invalidation that refreshes the task should refresh it. */
  taskRuns: (taskId: string) => ['task', taskId, 'runs'] as const,
  run: (runId: string) => ['run', runId] as const,
  /** + M8. The report a run wrote — a run's own key, since a project-scoped run has no
   * task to nest it under. */
  runReport: (runId: string) => ['run', runId, 'report'] as const,
};

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
          if (error instanceof ApiError && error.isClientError) return false;
          return failureCount < 3;
        },
      },
      mutations: { retry: false },
    },
  });
}

// --- queries --------------------------------------------------------------------------

export function useMe() {
  return useQuery({ queryKey: queryKeys.me, queryFn: api.getMe });
}

export function useProjects(status?: Project['status']) {
  return useQuery({
    queryKey: status ? [...queryKeys.projects, status] : queryKeys.projects,
    queryFn: () => api.listProjects(status),
  });
}

export function useProject(projectId: string) {
  return useQuery({
    queryKey: queryKeys.project(projectId),
    queryFn: () => api.getProject(projectId),
  });
}

export function useEffectivePrefs(projectId: string) {
  return useQuery({
    queryKey: queryKeys.effectivePrefs(projectId),
    queryFn: () => api.getEffectivePrefs(projectId),
  });
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
  });
}

// --- mutations ------------------------------------------------------------------------

/** Apply `patch` to one task wherever it sits in the nested board. */
function patchBoardTask(
  board: TaskWithSubtasks[],
  taskId: string,
  patch: Partial<Task>,
): TaskWithSubtasks[] {
  return board.map((parent) => {
    if (parent.id === taskId) return { ...parent, ...patch };
    if (!parent.subtasks.some((child) => child.id === taskId)) return parent;
    return {
      ...parent,
      subtasks: parent.subtasks.map((child) =>
        child.id === taskId ? { ...child, ...patch } : child,
      ),
    };
  });
}

interface BoardMutationContext {
  previous: TaskWithSubtasks[] | undefined;
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
  const queryClient = useQueryClient();
  const key = queryKeys.tasks(projectId, filters);

  return useMutation<unknown, Error, TVariables, BoardMutationContext>({
    mutationFn,
    async onMutate(variables) {
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<TaskWithSubtasks[]>(key);
      if (previous) {
        queryClient.setQueryData<TaskWithSubtasks[]>(key, optimisticPatch(previous, variables));
      }
      return { previous };
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

export function useSetTaskState(projectId: string, filters: BoardFilters) {
  return useOptimisticBoardMutation<{
    taskId: string;
    state: TaskState;
    postponedUntil?: string | null;
  }>(
    projectId,
    filters,
    ({ taskId, state, postponedUntil }) => api.setTaskState(taskId, state, postponedUntil),
    (board, { taskId, state, postponedUntil }) => {
      const patched = patchBoardTask(board, taskId, {
        state,
        postponedUntil: postponedUntil ?? null,
      });
      // Until M4 this also demoted whatever else was `current`, mirroring a server
      // invariant that no longer exists: `in_progress` is not singular, and starting a
      // task demotes nothing (docs/02-data-model.md#task-state-machine). Deleting the
      // mirror rather than leaving it is the point — an optimistic update that models a
      // rule the server has dropped is a UI that briefly lies and then corrects itself.
      return patched;
    },
  );
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
  const queryClient = useQueryClient();
  const key = queryKeys.task(parentTaskId);

  return useMutation<
    unknown,
    Error,
    { taskId: string; state: TaskState; postponedUntil?: string | null },
    { previous: TaskDetail | undefined }
  >({
    mutationFn: ({ taskId, state, postponedUntil }) =>
      api.setTaskState(taskId, state, postponedUntil),
    async onMutate({ taskId, state, postponedUntil }) {
      await queryClient.cancelQueries({ queryKey: key, exact: true });
      const previous = queryClient.getQueryData<TaskDetail>(key);
      if (previous) {
        queryClient.setQueryData<TaskDetail>(key, {
          ...previous,
          task: {
            ...previous.task,
            subtasks: previous.task.subtasks.map((child) =>
              child.id === taskId
                ? { ...child, state, postponedUntil: postponedUntil ?? null }
                : child,
            ),
          },
        });
      }
      return { previous };
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: key, exact: true });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/**
 * Ticking, editing, and removing a checklist item.
 *
 * One hook for all three because they share the interesting part: the server answers with
 * the **whole task**, since a checklist write can complete the task (invariant 6) and move
 * the project's counts. So `onSuccess` writes the returned task into `['task', id]` rather
 * than invalidating and waiting — the checkbox and the state badge move together, which is
 * the point of returning the task at all.
 *
 * The optimistic patch covers only `completed`, and only on the item. Deriving the task's
 * state here would mean a second implementation of `derive_state` in TypeScript, kept in
 * step by hand, to save one round trip — and getting it subtly wrong would show the learner
 * a task completing and then un-completing.
 */
export function useTaskItemMutation(taskId: string, projectId: string) {
  const queryClient = useQueryClient();
  const key = queryKeys.task(taskId);

  return useMutation<
    TaskMutation,
    Error,
    | { kind: 'toggle'; itemId: string; completed: boolean }
    | { kind: 'delete'; itemId: string }
    | { kind: 'add'; items: { shortDescription: string; guided?: boolean }[] },
    { previous: TaskDetail | undefined }
  >({
    mutationFn: (variables) => {
      if (variables.kind === 'toggle') {
        return api.patchTaskItem(taskId, variables.itemId, { completed: variables.completed });
      }
      if (variables.kind === 'delete') return api.deleteTaskItem(taskId, variables.itemId);
      return api.addTaskItems(taskId, variables.items, newIdempotencyKey());
    },
    async onMutate(variables) {
      if (variables.kind !== 'toggle') return { previous: undefined };
      await queryClient.cancelQueries({ queryKey: key, exact: true });
      const previous = queryClient.getQueryData<TaskDetail>(key);
      if (previous) {
        queryClient.setQueryData<TaskDetail>(key, {
          ...previous,
          task: {
            ...previous.task,
            items: previous.task.items.map((item) =>
              item.itemId === variables.itemId
                ? { ...item, completed: variables.completed }
                : item,
            ),
          },
        });
      }
      return { previous };
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSuccess(result) {
      const current = queryClient.getQueryData<TaskDetail>(key);
      if (current) {
        queryClient.setQueryData<TaskDetail>(key, {
          ...current,
          task: { ...current.task, ...result.task, subtasks: current.task.subtasks },
        });
      }
    },
    onSettled() {
      // The board shows "2 of 5 done" and the task's state badge, and the project's counts
      // move when a task completes — both live under keys this write can change.
      void queryClient.invalidateQueries({ queryKey: key, exact: true });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/**
 * Create one subtask, from the parent's workspace.
 *
 * The hand path, and since `POST /api/tasks/{id}/split` was removed the only one. It
 * invalidates the parent's detail as well as the board because the write changes *both*
 * ends: the child appears under `subtasks[]`, and if the parent had a checklist it has just
 * moved onto that child (docs/02-data-model.md#task-items).
 */
export function useCreateSubtask(parentTaskId: string, projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; estimatedMinutes: number; parentTaskId: string }) =>
      api.createTask(projectId, body, newIdempotencyKey()),
    onSettled() {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.task(parentTaskId),
        exact: true,
      });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/**
 * Ticking an item on a **subtask**, from inside the parent's workspace.
 *
 * Separate from `useTaskItemMutation` for the same reason `useSetSubtaskState` is separate
 * from `useSetTaskState`: the write is against a different task from the one this screen is
 * keyed on. The response is the *subtask*, so it is patched into the parent's `subtasks[]`
 * rather than over `task` — writing it to the wrong place would replace the parent with its
 * child, which renders as the workspace suddenly being about something else.
 *
 * A subtask completing is also the parent's business: its `rollup` moves on the same write,
 * and the ring above the cards reads that. Hence the invalidation as well as the patch.
 */
export function useSubtaskItemMutation(parentTaskId: string, projectId: string) {
  const queryClient = useQueryClient();
  const key = queryKeys.task(parentTaskId);

  return useMutation<
    TaskMutation,
    Error,
    { taskId: string; itemId: string; completed: boolean },
    { previous: TaskDetail | undefined }
  >({
    mutationFn: ({ taskId, itemId, completed }) =>
      api.patchTaskItem(taskId, itemId, { completed }),
    async onMutate({ taskId, itemId, completed }) {
      await queryClient.cancelQueries({ queryKey: key, exact: true });
      const previous = queryClient.getQueryData<TaskDetail>(key);
      if (previous) {
        queryClient.setQueryData<TaskDetail>(key, {
          ...previous,
          task: {
            ...previous.task,
            subtasks: previous.task.subtasks.map((child) =>
              child.id === taskId
                ? {
                    ...child,
                    items: child.items.map((item) =>
                      item.itemId === itemId ? { ...item, completed } : item,
                    ),
                  }
                : child,
            ),
          },
        });
      }
      return { previous };
    },
    onError(_error, _variables, context) {
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: key, exact: true });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/**
 * Registers a just-started research run's turn with the stream store and the socket, so
 * the research view's `SessionPane` has something to show the moment it mounts.
 *
 * Shared by the task-scoped and the project-scoped trigger below: since M8 a research
 * turn always runs in the run's own fresh session (`run.sessionId`), never the session the
 * request was made from, so both callers register the same way.
 */
function subscribeToResearchRun(run: { turnId: string | null; sessionId: string }): void {
  if (!run.turnId) return;
  useStreamStore.getState().begin(run.turnId, run.sessionId);
  getSocket().subscribe(run.turnId);
}

/**
 * "Research this task now".
 *
 * Answers 202 with a `runId` and the run's own `sessionId` — since M8 the caller
 * navigates to that run's research view to watch it
 * (docs/06-frontend.md#research-view-projectsprojectidresearchrunid) rather than seeing
 * chips inline here. A `409` means the project's agent lease is held and carries the
 * in-flight `runId` in its problem document; the caller surfaces that rather than
 * inviting a second press.
 */
export function useStartResearch(taskId: string, projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sessionId, force }: { sessionId: string; force?: boolean }) =>
      api.startResearch(sessionId, { force }, newIdempotencyKey()),
    onSuccess: subscribeToResearchRun,
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(taskId), exact: true });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.taskRuns(taskId) });
    },
  });
}

/**
 * Research kicked off from the project coach's own conversation, about the project as a
 * whole rather than one task — the M8 capability. `reason` is the question to research,
 * and the server refuses an empty one: there is no task description to fall back on.
 */
export function useStartProjectResearch(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      sessionId,
      reason,
      attachments,
    }: {
      sessionId: string;
      reason: string;
      attachments?: { uploadId: string; mimeType: string }[];
    }) => api.startResearch(sessionId, { reason, attachments }, newIdempotencyKey()),
    onSuccess: subscribeToResearchRun,
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.projectRuns(projectId) });
    },
  });
}

/**
 * "Have my coach prepare this" — queue research for the next tick instead of watching it.
 *
 * The queued, headless half of the research pair
 * (docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid). It answers with the
 * whole task, so the cache is *set* rather than only invalidated: the "Starts soon" badge
 * has to appear on the press, and a refetch round trip is long enough for a second click.
 *
 * `queued` picks the verb. One mutation rather than two because the control is one toggle,
 * and splitting it would let a component call the wrong half of a pair whose whole job is
 * to be each other's inverse.
 */
export function useResearchRequest(taskId: string, projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ queued }: { queued: boolean }) =>
      queued
        ? api.requestResearch(taskId, newIdempotencyKey())
        : api.cancelResearchRequest(taskId),
    onSuccess(mutation) {
      queryClient.setQueryData(queryKeys.task(taskId), (previous: TaskDetail | undefined) =>
        previous ? { ...previous, task: { ...previous.task, ...mutation.task } } : previous,
      );
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(taskId), exact: true });
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
    },
  });
}

/**
 * Recent runs for the project. Drives the "Updated by your coach" banner.
 *
 * No polling: a run that changes the board pushes `board_update`, which invalidates the
 * `['project', id]` prefix this key sits under. A timer would be a second mechanism for
 * something the socket already reports, and would keep running on a board nothing is
 * happening to.
 */
export function useProjectRuns(projectId: string) {
  return useQuery({
    queryKey: queryKeys.projectRuns(projectId),
    queryFn: () => api.listProjectRuns(projectId),
    enabled: projectId.length > 0,
  });
}

/**
 * The banner's one-click undo (docs/05-autonomous-runs.md#what-the-run-is-allowed-to-change).
 *
 * Invalidates by the ids the *server* says it touched rather than by the run's `changes`:
 * undo tolerates a task that has already gone, so what it did touch is a strictly smaller
 * list than what the run changed.
 */
export function useUndoRun(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => api.undoRun(runId, newIdempotencyKey()),
    onSuccess(result) {
      for (const taskId of result.taskIds) {
        void queryClient.invalidateQueries({ queryKey: ['task', taskId], exact: true });
      }
    },
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

/**
 * Recent runs for one task. Drives the task workspace's "Latest research" card, the same
 * way `useProjectRuns` drives the board's.
 */
export function useTaskRuns(taskId: string) {
  return useQuery({
    queryKey: queryKeys.taskRuns(taskId),
    queryFn: () => api.listTaskRuns(taskId),
    enabled: taskId.length > 0,
  });
}

/**
 * One run, for the research view. Polls while `running`: unlike the board and the task
 * workspace, this screen has no `board_update` to tell it the run finished — a
 * project-scoped run touches no task, so nothing pushes an invalidation here.
 */
export function useRun(runId: string) {
  return useQuery({
    queryKey: queryKeys.run(runId),
    queryFn: () => api.getRun(runId),
    enabled: runId.length > 0,
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 2000 : false),
  });
}

/**
 * The report a run wrote. `enabled` is the caller's `run.status === 'complete'`: a run
 * only reaches `complete` after `post_report` succeeds, so a report is there to fetch —
 * and fetching earlier would be a 404 the research view has nothing useful to do with.
 */
export function useRunReport(runId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.runReport(runId),
    queryFn: () => api.getRunReport(runId),
    enabled: enabled && runId.length > 0,
  });
}

export function useReportFeedback(taskId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      reportId,
      itemId,
      feedback,
    }: {
      reportId: string;
      itemId: string;
      feedback: 'up' | 'down' | null;
    }) => api.setReportItemFeedback(reportId, itemId, taskId, feedback),
    onSettled() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(taskId), exact: true });
    },
  });
}

export function useReorderTask(projectId: string, filters: BoardFilters) {
  return useOptimisticBoardMutation<{
    taskId: string;
    targetIndex: number;
    afterTaskId?: string;
    beforeTaskId?: string;
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
      const order = orderForMove(board, taskId, targetIndex);
      return patchBoardTask(board, taskId, { order }).sort((a, b) =>
        a.order < b.order ? -1 : a.order > b.order ? 1 : 0,
      );
    },
  );
}

export function useCreateTask(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; estimatedMinutes: number; afterTaskId?: string }) =>
      api.createTask(projectId, body, newIdempotencyKey()),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: ['tasks', projectId] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
    },
  });
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { title: string; goal?: string }) =>
      api.createProject(body, newIdempotencyKey()),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
}

export function usePatchProject(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (patch: {
      title?: string;
      goal?: string;
      status?: Project['status'];
      prefs?: Partial<ProjectPrefs>;
    }) => api.patchProject(projectId, patch),
    onSuccess() {
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.effectivePrefs(projectId) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.projects });
    },
  });
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
  });
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
  });
}

export function useTask(taskId: string) {
  return useQuery({
    queryKey: queryKeys.task(taskId),
    queryFn: () => api.getTask(taskId),
    enabled: taskId.length > 0,
  });
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
      const collected: SessionEvent[] = [];
      let cursor = 0;
      for (;;) {
        const page = await api.listSessionEvents(sessionId, cursor, 100);
        collected.push(...page.events);
        if (!page.hasMore || page.nextAfterSeq === cursor) break;
        cursor = page.nextAfterSeq;
      }
      return collected;
    },
  });
}

export interface StartTurnBody {
  text: string;
  /** `filename` is used only by the optimistic echo; it is not sent to the server. */
  attachments?: { uploadId: string; mimeType: string; filename?: string }[];
  /** The answer to a tool that asked first. A turn may carry this and nothing else. */
  confirmation?: {
    functionCallId: string;
    confirmed: boolean;
    payload?: Record<string, unknown>;
  };
}

/**
 * The synthetic event that stands in for a message the server has not stored yet.
 *
 * `seq` is one past the highest known, so it sorts last; the id is marked `pending:` so
 * it cannot collide with an ADK event id.
 */
export function pendingUserEvent(existing: SessionEvent[], body: StartTurnBody): SessionEvent {
  const parts: Record<string, unknown>[] = [];
  if (body.text) parts.push({ text: body.text });
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
    });
  }
  const highest = existing.reduce((max, event) => Math.max(max, event.seq), 0);
  return {
    seq: highest + 1,
    eventId: `pending:${crypto.randomUUID()}`,
    event: { author: 'user', content: { role: 'user', parts } },
  };
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
  const queryClient = useQueryClient();
  const key = queryKeys.sessionEvents(sessionId);

  return useMutation<
    Awaited<ReturnType<typeof api.startTurn>>,
    Error,
    StartTurnBody,
    { previous: SessionEvent[] | undefined }
  >({
    mutationFn: (body) => api.startTurn(sessionId, body, newIdempotencyKey()),
    onMutate(body) {
      const previous = queryClient.getQueryData<SessionEvent[]>(key);
      queryClient.setQueryData<SessionEvent[]>(key, (current) => [
        ...(current ?? []),
        pendingUserEvent(current ?? [], body),
      ]);
      return { previous };
    },
    onError(_error, _body, context) {
      // The message was never accepted, so it must not sit in the transcript looking as
      // though it had been.
      if (context?.previous) queryClient.setQueryData(key, context.previous);
    },
    onSuccess(turn) {
      // Register the turn before any frame arrives, so a socket that reconnects in the
      // gap between the 202 and the first delta still has something to resume.
      useStreamStore.getState().begin(turn.turnId, turn.sessionId);
      getSocket().subscribe(turn.turnId);
    },
  });
}

export function useCancelTurn(sessionId: string) {
  return useMutation({
    mutationFn: (turnId: string) => api.cancelTurn(sessionId, turnId),
  });
}

export function usePatchGlobalPrefs() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<GlobalPrefs>) => api.patchPrefs(patch),
    onSuccess(me) {
      queryClient.setQueryData(queryKeys.me, me);
      // Every project's effective prefs may have moved with the global layer.
      void queryClient.invalidateQueries({ queryKey: ['project'] });
    },
  });
}

/** `PATCH /api/me` — the display name shown in the header instead of the signed-in email. */
export function usePatchDisplayName() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (displayName: string) => api.patchDisplayName(displayName, newIdempotencyKey()),
    onSuccess(me) {
      queryClient.setQueryData(queryKeys.me, me);
    },
  });
}

/** `POST /api/coupons/claim`. M8-quotas — replaces `plan.limits` on success. */
export function useClaimCoupon() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (code: string) => api.claimCoupon(code, newIdempotencyKey()),
    onSuccess(response) {
      queryClient.setQueryData<Me>(queryKeys.me, (previous) =>
        previous ? { ...previous, plan: response.plan } : previous,
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.me });
    },
  });
}

export function usePatchLearnerProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (patch: Parameters<typeof api.patchLearnerProfile>[0]) =>
      api.patchLearnerProfile(patch),
    onSuccess(profile) {
      queryClient.setQueryData<Me>(queryKeys.me, (previous) =>
        previous ? { ...previous, learnerProfile: profile } : previous,
      );
      void queryClient.invalidateQueries({ queryKey: queryKeys.me });
    },
  });
}
