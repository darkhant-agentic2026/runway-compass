/**
 * The API client.
 *
 * Same origin in every environment — the SPA ships inside the API image
 * (docs/01-architecture.md) — so paths are relative and there is no base URL to
 * configure. In local development Vite proxies `/api` to the API process; see
 * `vite.config.ts`.
 *
 * Every response is parsed through a Zod schema, and every error arrives as RFC 9457
 * `application/problem+json` and is raised as `ApiError` carrying the parsed problem.
 */

import type { z } from 'zod'

import { getAuthProvider } from '@/lib/auth'
import {
  boardSchema,
  effectivePrefsResponseSchema,
  meSchema,
  problemSchema,
  projectListSchema,
  projectSchema,
  taskDetailSchema,
  taskMutationSchema,
  type GlobalPrefs,
  type Project,
  type ProjectPrefs,
  type TaskState,
} from '@/lib/schemas'

export class ApiError extends Error {
  readonly status: number
  readonly problem: { type: string; title: string; status: number; detail: string }

  constructor(status: number, problem: ApiError['problem']) {
    super(problem.detail || problem.title)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
  }

  /** 4xx responses are the caller's fault and must not be retried. */
  get isClientError(): boolean {
    return this.status >= 400 && this.status < 500
  }
}

interface RequestOptions {
  method?: string
  body?: unknown
  /** Sent as `Idempotency-Key`; every mutating endpoint accepts one. */
  idempotencyKey?: string
  signal?: AbortSignal
}

async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  options: RequestOptions = {},
): Promise<T> {
  const token = await getAuthProvider().getIdToken()
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  const response = await fetch(path, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    ...(options.signal ? { signal: options.signal } : {}),
  })

  if (!response.ok) {
    let problem = {
      type: 'about:blank',
      title: response.statusText || 'Request failed',
      status: response.status,
      detail: '',
    }
    try {
      const parsed = problemSchema.safeParse(await response.json())
      if (parsed.success) problem = { ...problem, ...parsed.data }
    } catch {
      /* a non-JSON error body leaves the status-derived problem above */
    }
    throw new ApiError(response.status, problem)
  }

  if (response.status === 204) return schema.parse(undefined)
  return schema.parse(await response.json())
}

/** A stable key per logical operation, so a retried mutation is deduplicated server-side. */
export function newIdempotencyKey(): string {
  return crypto.randomUUID()
}

// --- identity & preferences -----------------------------------------------------------

export const api = {
  getMe: () => request('/api/me', meSchema),

  patchPrefs: (patch: Partial<GlobalPrefs>, idempotencyKey?: string) =>
    request('/api/me/prefs', meSchema, {
      method: 'PATCH',
      body: patch,
      ...(idempotencyKey ? { idempotencyKey } : {}),
    }),

  // --- projects ---------------------------------------------------------------------

  listProjects: (status?: Project['status']) =>
    request(
      status ? `/api/projects?status=${encodeURIComponent(status)}` : '/api/projects',
      projectListSchema,
    ).then((response) => response.projects),

  createProject: (body: { title: string; goal?: string }, idempotencyKey?: string) =>
    request('/api/projects', projectSchema, {
      method: 'POST',
      body,
      ...(idempotencyKey ? { idempotencyKey } : {}),
    }),

  getProject: (projectId: string) => request(`/api/projects/${projectId}`, projectSchema),

  patchProject: (
    projectId: string,
    patch: {
      title?: string
      goal?: string
      status?: Project['status']
      prefs?: Partial<ProjectPrefs>
    },
  ) => request(`/api/projects/${projectId}`, projectSchema, { method: 'PATCH', body: patch }),

  archiveProject: (projectId: string) =>
    request(`/api/projects/${projectId}`, projectSchema, { method: 'DELETE' }),

  getEffectivePrefs: (projectId: string) =>
    request(`/api/projects/${projectId}/effective-prefs`, effectivePrefsResponseSchema).then(
      (response) => response.effectivePrefs,
    ),

  // --- tasks ------------------------------------------------------------------------

  listTasks: (
    projectId: string,
    filters: {
      includeCompleted?: boolean
      includeDiscarded?: boolean
      includePostponed?: boolean
    } = {},
  ) => {
    const query = new URLSearchParams()
    if (filters.includeCompleted !== undefined) {
      query.set('include_completed', String(filters.includeCompleted))
    }
    if (filters.includeDiscarded !== undefined) {
      query.set('include_discarded', String(filters.includeDiscarded))
    }
    if (filters.includePostponed !== undefined) {
      query.set('include_postponed', String(filters.includePostponed))
    }
    const suffix = query.size > 0 ? `?${query.toString()}` : ''
    return request(`/api/projects/${projectId}/tasks${suffix}`, boardSchema).then(
      (response) => response.tasks,
    )
  },

  createTask: (
    projectId: string,
    body: {
      title: string
      description?: string
      estimatedMinutes?: number
      parentTaskId?: string | null
      afterTaskId?: string | null
      needsResearch?: boolean
    },
    idempotencyKey?: string,
  ) =>
    request(`/api/projects/${projectId}/tasks`, taskMutationSchema, {
      method: 'POST',
      body,
      ...(idempotencyKey ? { idempotencyKey } : {}),
    }),

  getTask: (taskId: string) =>
    request(`/api/tasks/${taskId}`, taskDetailSchema).then((response) => response.task),

  patchTask: (
    taskId: string,
    patch: {
      title?: string
      description?: string
      estimatedMinutes?: number
      needsResearch?: boolean
    },
  ) => request(`/api/tasks/${taskId}`, taskMutationSchema, { method: 'PATCH', body: patch }),

  setTaskState: (taskId: string, state: TaskState, postponedUntil?: string | null) =>
    request(`/api/tasks/${taskId}/state`, taskMutationSchema, {
      method: 'POST',
      body: { state, postponedUntil: postponedUntil ?? null },
    }),

  reorderTask: (taskId: string, anchor: { afterTaskId?: string; beforeTaskId?: string }) =>
    request(`/api/tasks/${taskId}/reorder`, taskMutationSchema, {
      method: 'POST',
      body: anchor,
    }),

  splitTask: (
    taskId: string,
    subtasks: { title: string; description?: string; estimatedMinutes: number }[],
  ) =>
    request(`/api/tasks/${taskId}/split`, taskDetailSchema, {
      method: 'POST',
      body: { subtasks },
    }).then((response) => response.task),
}
