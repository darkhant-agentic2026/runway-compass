/**
 * Optimistic mutations.
 *
 * docs/08-testing.md:
 *
 * > **Optimistic mutations** — complete/postpone/reorder patch the cache immediately and
 * > roll back on a 500; the optimistic fractional index equals the server's.
 */

import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createQueryClient,
  queryKeys,
  useReorderTask,
  useSetSubtaskState,
  useSetTaskState,
  useStartTurn,
} from '@/features/queries';
import { api, ApiError } from '@/lib/api';
import { keyBetween } from '@/lib/ordering';
import type { SessionEvent, TaskDetail, TaskWithSubtasks } from '@/lib/schemas';
import { getSocket } from '@/lib/socket';
import { toMessages } from '@/lib/transcript';
import { DEFAULT_FILTERS } from '@/stores/boardUi';
import { makeParent, makeTask } from '@/test/factories';

const PROJECT_ID = 'p_1';

function setup(board: TaskWithSubtasks[]) {
  const queryClient = createQueryClient();
  queryClient.setQueryData(queryKeys.tasks(PROJECT_ID, DEFAULT_FILTERS), board);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const read = () =>
    queryClient.getQueryData<TaskWithSubtasks[]>(queryKeys.tasks(PROJECT_ID, DEFAULT_FILTERS))!;
  return { queryClient, wrapper, read };
}

afterEach(() => vi.restoreAllMocks());

describe('useSetTaskState', () => {
  let board: TaskWithSubtasks[];

  beforeEach(() => {
    board = [
      makeParent({ id: 'k_a', order: 'a0', state: 'in_progress' }),
      makeParent({ id: 'k_b', order: 'a1', state: 'not_started' }),
    ];
  });

  it('patches the cache before the request resolves', async () => {
    let resolve: (() => void) | undefined;
    vi.spyOn(api, 'setTaskState').mockReturnValue(
      new Promise((r) => {
        resolve = () => r({ task: makeTask(), parent: null, project: null });
      }),
    );

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_a', state: 'completed' });

    await waitFor(() => {
      expect(read().find((task) => task.id === 'k_a')?.state).toBe('completed');
    });
    resolve?.();
  });

  it('rolls back on a server error', async () => {
    vi.spyOn(api, 'setTaskState').mockRejectedValue(new Error('boom'));

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_b', state: 'in_progress' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(read().find((task) => task.id === 'k_b')?.state).toBe('not_started');
    expect(read().find((task) => task.id === 'k_a')?.state).toBe('in_progress');
  });

  it('starting one task leaves the others in progress', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_b', state: 'in_progress' });

    await waitFor(() => {
      // The removed mirror, asserted as an absence. This hook used to demote whatever
      // else was `current`, matching a server invariant that M4 deleted — an optimistic
      // update modelling a rule the server no longer has is a UI that lies for a moment
      // and then corrects itself (docs/02-data-model.md#task-state-machine).
      expect(read().find((task) => task.id === 'k_a')?.state).toBe('in_progress');
      expect(read().find((task) => task.id === 'k_b')?.state).toBe('in_progress');
    });
  });

  it('carries postponedUntil into the optimistic patch', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });
    const when = '2027-01-01T00:00:00.000Z';

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_a', state: 'postponed_until', postponedUntil: when });

    await waitFor(() => {
      const task = read().find((entry) => entry.id === 'k_a');
      expect(task?.state).toBe('postponed_until');
      expect(task?.postponedUntil).toBe(when);
    });
  });
});

describe('useSetSubtaskState', () => {
  const PARENT = 'k_parent';

  function setupDetail() {
    const queryClient = createQueryClient();
    const parent = makeParent({ id: PARENT }, [
      makeTask({ id: 'k_one', state: 'in_progress' }),
      makeTask({ id: 'k_two', state: 'not_started' }),
    ]);
    // `['task', id]` holds the whole `GET /api/tasks/{id}` response from M4 — the task
    // *and* its latest report — because the workspace renders both.
    queryClient.setQueryData(queryKeys.task(PARENT), { task: parent, latestReport: null });
    // The get-or-create POST that shares the `['task', id]` prefix. It is here precisely
    // so the invalidation below can be observed to leave it alone.
    queryClient.setQueryData(queryKeys.taskSession(PARENT), { id: 's_1' });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const read = () => queryClient.getQueryData<TaskDetail>(queryKeys.task(PARENT))!.task;
    return { queryClient, wrapper, read };
  }

  it('patches the workspace’s own cache entry, which the board mutation never touches', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });

    const { wrapper, read } = setupDetail();
    const { result } = renderHook(() => useSetSubtaskState(PARENT, PROJECT_ID), { wrapper });

    result.current.mutate({ taskId: 'k_one', state: 'completed' });

    await waitFor(() => {
      expect(read().subtasks.find((task) => task.id === 'k_one')?.state).toBe('completed');
    });
  });

  it('rolls back on a server error', async () => {
    vi.spyOn(api, 'setTaskState').mockRejectedValue(new Error('boom'));

    const { wrapper, read } = setupDetail();
    const { result } = renderHook(() => useSetSubtaskState(PARENT, PROJECT_ID), { wrapper });

    result.current.mutate({ taskId: 'k_two', state: 'in_progress' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(read().subtasks.find((task) => task.id === 'k_two')?.state).toBe('not_started');
    expect(read().subtasks.find((task) => task.id === 'k_one')?.state).toBe('in_progress');
  });

  it('invalidates the task detail without disturbing the session under the same prefix', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });

    const { wrapper, queryClient } = setupDetail();
    const { result } = renderHook(() => useSetSubtaskState(PARENT, PROJECT_ID), { wrapper });

    result.current.mutate({ taskId: 'k_two', state: 'in_progress' });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The decision rather than the result (CLAUDE.md): `queryKeys.taskSession` is
    // `['task', id, 'session']`, so a *prefix* invalidation here would re-POST
    // `POST /api/tasks/{id}/session` on every click on a subtask's Complete button. Both
    // spellings pass a test that only checks the rows updated.
    expect(queryClient.getQueryState(queryKeys.task(PARENT))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(queryKeys.taskSession(PARENT))?.isInvalidated).toBe(false);
  });
});

describe('useStartTurn', () => {
  const SESSION = 's_1';
  const key = queryKeys.sessionEvents(SESSION);

  function setupSession() {
    const queryClient = createQueryClient();
    queryClient.setQueryData(key, []);
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    return { queryClient, wrapper, read: () => queryClient.getQueryData<SessionEvent[]>(key)! };
  }

  it('echoes the message into the transcript before the server confirms it', async () => {
    // Without this the sender watches "Your coach is thinking…" with no record of what
    // they asked: ADK writes the user event during generation, and the transcript is only
    // refetched on `turn_complete`.
    let resolve: (() => void) | undefined;
    vi.spyOn(api, 'startTurn').mockReturnValue(
      new Promise((r) => {
        resolve = () =>
          r({ turnId: 't_1', sessionId: SESSION, status: 'running', startSeq: 0 });
      }),
    );
    vi.spyOn(getSocket(), 'subscribe').mockImplementation(() => {});

    const { wrapper, read } = setupSession();
    const { result } = renderHook(() => useStartTurn(SESSION), { wrapper });

    result.current.mutate({ text: 'why does this deadlock?' });

    await waitFor(() => expect(read()).toHaveLength(1));
    expect(toMessages(read())[0]).toMatchObject({
      role: 'user',
      text: 'why does this deadlock?',
    });
    resolve?.();
  });

  it('shows the attachment on the echoed message, by name', async () => {
    vi.spyOn(api, 'startTurn').mockResolvedValue({
      turnId: 't_1',
      sessionId: SESSION,
      status: 'running',
      startSeq: 0,
    });
    vi.spyOn(getSocket(), 'subscribe').mockImplementation(() => {});

    const { wrapper, read } = setupSession();
    const { result } = renderHook(() => useStartTurn(SESSION), { wrapper });

    result.current.mutate({
      text: 'explain this',
      attachments: [{ uploadId: 'up_1', mimeType: 'image/png', filename: 'shot.png' }],
    });

    await waitFor(() => expect(read()).toHaveLength(1));
    expect(toMessages(read())[0]?.attachments).toEqual([
      { mimeType: 'image/png', filename: 'shot.png' },
    ]);
  });

  it('sorts the echo after everything already in the transcript', async () => {
    vi.spyOn(api, 'startTurn').mockResolvedValue({
      turnId: 't_1',
      sessionId: SESSION,
      status: 'running',
      startSeq: 0,
    });
    vi.spyOn(getSocket(), 'subscribe').mockImplementation(() => {});

    const { queryClient, wrapper, read } = setupSession();
    queryClient.setQueryData(key, [
      {
        seq: 7,
        eventId: 'e_7',
        event: { author: 'coach', content: { parts: [{ text: 'earlier' }] } },
      },
    ]);
    const { result } = renderHook(() => useStartTurn(SESSION), { wrapper });

    result.current.mutate({ text: 'later' });

    await waitFor(() => expect(read()).toHaveLength(2));
    expect(toMessages(read()).map((message) => message.text)).toEqual(['earlier', 'later']);
  });

  it('removes the echo when the server refuses the turn', async () => {
    // A message that was never accepted must not sit in the transcript looking as if it
    // had been.
    vi.spyOn(api, 'startTurn').mockRejectedValue(new Error('boom'));

    const { wrapper, read } = setupSession();
    const { result } = renderHook(() => useStartTurn(SESSION), { wrapper });

    result.current.mutate({ text: 'this will fail' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(read()).toEqual([]);
  });

  it('surfaces a quota-exceeded ApiError as the mutation error', async () => {
    // M8-quotas: a blocked turn is never created, so the caller (SessionPane's retry
    // banner) has to learn about it through the mutation's own error, not through a
    // `turn_error` frame that will never arrive.
    const problem = {
      type: '/problems/quota-exceeded',
      title: 'Usage quota exceeded',
      status: 429,
      detail: 'Your daily usage quota is exhausted. It resets at 2026-08-25T00:00:00+00:00.',
      window: 'daily',
      resetAt: '2026-08-25T00:00:00+00:00',
    };
    vi.spyOn(api, 'startTurn').mockRejectedValue(new ApiError(429, problem));

    const { wrapper } = setupSession();
    const { result } = renderHook(() => useStartTurn(SESSION), { wrapper });

    result.current.mutate({ text: 'hello' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    const error = result.current.error;
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).problem.type).toBe('/problems/quota-exceeded');
    expect((error as ApiError).problem.window).toBe('daily');
  });
});

describe('useReorderTask', () => {
  const board = [
    makeParent({ id: 'k_a', order: 'a0' }),
    makeParent({ id: 'k_b', order: 'a1' }),
    makeParent({ id: 'k_c', order: 'a2' }),
  ];

  it('computes the same fractional index the server will', async () => {
    const spy = vi.spyOn(api, 'reorderTask').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    // Move `k_c` between `k_a` and `k_b`.
    result.current.mutate({ taskId: 'k_c', targetIndex: 1, afterTaskId: 'k_a' });

    await waitFor(() => {
      const moved = read().find((task) => task.id === 'k_c');
      expect(moved?.order).toBe(keyBetween('a0', 'a1'));
    });
    expect(spy).toHaveBeenCalledWith('k_c', { afterTaskId: 'k_a' });
  });

  it('re-sorts the cached board so the row lands where it was dropped', async () => {
    vi.spyOn(api, 'reorderTask').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    });

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_c', targetIndex: 0, beforeTaskId: 'k_a' });

    await waitFor(() => {
      expect(read().map((task) => task.id)).toEqual(['k_c', 'k_a', 'k_b']);
    });
  });

  it('restores the original order when the request fails', async () => {
    vi.spyOn(api, 'reorderTask').mockRejectedValue(new Error('boom'));

    const { wrapper, read } = setup(board);
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    });

    result.current.mutate({ taskId: 'k_c', targetIndex: 0, beforeTaskId: 'k_a' });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(read().map((task) => task.id)).toEqual(['k_a', 'k_b', 'k_c']);
  });
});

describe('board_update reaches the screens that read a single task', () => {
  const TASK = 'k_researched';
  const PROJECT = 'p_1';

  function setupCaches() {
    const queryClient = createQueryClient();
    queryClient.setQueryData(queryKeys.task(TASK), {
      task: makeParent({ id: TASK }),
      latestReport: null,
    });
    queryClient.setQueryData(queryKeys.taskRuns(TASK), []);
    queryClient.setQueryData(queryKeys.taskSession(TASK), { id: 's_1' });
    queryClient.setQueryData(queryKeys.tasks(PROJECT, DEFAULT_FILTERS), []);
    return queryClient;
  }

  /**
   * `useCoachSocket`'s handler, lifted out so this can assert the invalidation without
   * standing up a socket. What is being pinned is the *decision* — which keys a frame
   * touches — not the transport, which `socket.test.ts` already covers.
   */
  function handle(queryClient: QueryClient, frame: { projectId: string; taskIds: string[] }) {
    void queryClient.invalidateQueries({ queryKey: ['tasks', frame.projectId] });
    void queryClient.invalidateQueries({ queryKey: ['project', frame.projectId] });
    for (const taskId of frame.taskIds) {
      void queryClient.invalidateQueries({ queryKey: ['task', taskId], exact: true });
      void queryClient.invalidateQueries({ queryKey: ['task', taskId, 'runs'] });
    }
  }

  it('invalidates the named task’s detail and its research runs, not just the board', () => {
    // The defect this pins: a research run posts its report and says nothing else, so
    // `board_update` is the *only* signal the workspace gets. Before M4 the frame
    // invalidated `['tasks', projectId]` and `['project', projectId]` — neither of which
    // the task's own screen reads — and the checklist simply never appeared. Since M8 the
    // same gap exists for `useTaskRuns`, which is what the "latest research" card reads.
    const queryClient = setupCaches();
    handle(queryClient, { projectId: PROJECT, taskIds: [TASK] });

    expect(queryClient.getQueryState(queryKeys.task(TASK))?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(queryKeys.taskRuns(TASK))?.isInvalidated).toBe(true);
    expect(
      queryClient.getQueryState(queryKeys.tasks(PROJECT, DEFAULT_FILTERS))?.isInvalidated,
    ).toBe(true);
  });

  it('leaves the get-or-create session under the same prefix alone', () => {
    // `queryKeys.taskSession` is `['task', id, 'session']`, so a prefix invalidation would
    // re-POST `POST /api/tasks/{id}/session` on every board update — including every one
    // an autonomous run produces at M5. The `exact` flag is what stops it, and both
    // spellings pass a test that only checks the checklist refreshed.
    const queryClient = setupCaches();
    handle(queryClient, { projectId: PROJECT, taskIds: [TASK] });

    expect(queryClient.getQueryState(queryKeys.taskSession(TASK))?.isInvalidated).toBe(false);
  });
});
