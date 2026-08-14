/**
 * Optimistic mutations.
 *
 * docs/08-testing.md:
 *
 * > **Optimistic mutations** — complete/postpone/reorder patch the cache immediately and
 * > roll back on a 500; the optimistic fractional index equals the server's.
 */

import { QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createQueryClient, queryKeys, useReorderTask, useSetTaskState } from '@/features/queries'
import { api } from '@/lib/api'
import { keyBetween } from '@/lib/ordering'
import type { TaskWithSubtasks } from '@/lib/schemas'
import { DEFAULT_FILTERS } from '@/stores/boardUi'
import { makeParent, makeTask } from '@/test/factories'

const PROJECT_ID = 'p_1'

function setup(board: TaskWithSubtasks[]) {
  const queryClient = createQueryClient()
  queryClient.setQueryData(queryKeys.tasks(PROJECT_ID, DEFAULT_FILTERS), board)
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  const read = () =>
    queryClient.getQueryData<TaskWithSubtasks[]>(queryKeys.tasks(PROJECT_ID, DEFAULT_FILTERS))!
  return { queryClient, wrapper, read }
}

afterEach(() => vi.restoreAllMocks())

describe('useSetTaskState', () => {
  let board: TaskWithSubtasks[]

  beforeEach(() => {
    board = [
      makeParent({ id: 'k_a', order: 'a0', state: 'current' }),
      makeParent({ id: 'k_b', order: 'a1', state: 'not_started' }),
    ]
  })

  it('patches the cache before the request resolves', async () => {
    let resolve: (() => void) | undefined
    vi.spyOn(api, 'setTaskState').mockReturnValue(
      new Promise((r) => {
        resolve = () => r({ task: makeTask(), parent: null, project: null })
      }),
    )

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_a', state: 'completed' })

    await waitFor(() => {
      expect(read().find((task) => task.id === 'k_a')?.state).toBe('completed')
    })
    resolve?.()
  })

  it('rolls back on a server error', async () => {
    vi.spyOn(api, 'setTaskState').mockRejectedValue(new Error('boom'))

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_b', state: 'current' })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(read().find((task) => task.id === 'k_b')?.state).toBe('not_started')
    expect(read().find((task) => task.id === 'k_a')?.state).toBe('current')
  })

  it('demotes the previous current task optimistically', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    })

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_b', state: 'current' })

    await waitFor(() => {
      // Mirroring the server's single-`current` invariant locally: without this the
      // board briefly shows two "Next up" rows.
      expect(read().find((task) => task.id === 'k_a')?.state).toBe('not_started')
      expect(read().find((task) => task.id === 'k_b')?.state).toBe('current')
    })
  })

  it('carries postponedUntil into the optimistic patch', async () => {
    vi.spyOn(api, 'setTaskState').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    })
    const when = '2027-01-01T00:00:00.000Z'

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useSetTaskState(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_a', state: 'postponed_until', postponedUntil: when })

    await waitFor(() => {
      const task = read().find((entry) => entry.id === 'k_a')
      expect(task?.state).toBe('postponed_until')
      expect(task?.postponedUntil).toBe(when)
    })
  })
})

describe('useReorderTask', () => {
  const board = [
    makeParent({ id: 'k_a', order: 'a0' }),
    makeParent({ id: 'k_b', order: 'a1' }),
    makeParent({ id: 'k_c', order: 'a2' }),
  ]

  it('computes the same fractional index the server will', async () => {
    const spy = vi.spyOn(api, 'reorderTask').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    })

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    // Move `k_c` between `k_a` and `k_b`.
    result.current.mutate({ taskId: 'k_c', targetIndex: 1, afterTaskId: 'k_a' })

    await waitFor(() => {
      const moved = read().find((task) => task.id === 'k_c')
      expect(moved?.order).toBe(keyBetween('a0', 'a1'))
    })
    expect(spy).toHaveBeenCalledWith('k_c', { afterTaskId: 'k_a' })
  })

  it('re-sorts the cached board so the row lands where it was dropped', async () => {
    vi.spyOn(api, 'reorderTask').mockResolvedValue({
      task: makeTask(),
      parent: null,
      project: null,
    })

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_c', targetIndex: 0, beforeTaskId: 'k_a' })

    await waitFor(() => {
      expect(read().map((task) => task.id)).toEqual(['k_c', 'k_a', 'k_b'])
    })
  })

  it('restores the original order when the request fails', async () => {
    vi.spyOn(api, 'reorderTask').mockRejectedValue(new Error('boom'))

    const { wrapper, read } = setup(board)
    const { result } = renderHook(() => useReorderTask(PROJECT_ID, DEFAULT_FILTERS), {
      wrapper,
    })

    result.current.mutate({ taskId: 'k_c', targetIndex: 0, beforeTaskId: 'k_a' })

    await waitFor(() => expect(result.current.isError).toBe(true))
    expect(read().map((task) => task.id)).toEqual(['k_a', 'k_b', 'k_c'])
  })
})
