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
  sessionEventsSchema,
  sessionResponseSchema,
  sessionSummarySchema,
  taskDetailSchema,
  taskMutationSchema,
  turnAcceptedSchema,
  turnStatusResponseSchema,
  uploadCreatedSchema,
  uploadFinalizedSchema,
  wsTicketSchema,
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

  // --- sessions & turns ---------------------------------------------------------------

  /**
   * Get-or-create the project's intake conversation.
   *
   * Added at M3 alongside the endpoint. A POST, and named like `openTaskSession`, because
   * it is the same get-or-create: opening the board twice must not fork the conversation.
   */
  openProjectSession: (projectId: string) =>
    request(`/api/projects/${projectId}/session`, sessionSummarySchema, { method: 'POST' }),

  /** Get-or-create. Every workspace open calls this, so it is not a create. */
  openTaskSession: (taskId: string) =>
    request(`/api/tasks/${taskId}/session`, sessionResponseSchema, { method: 'POST' }).then(
      (response) => response.session,
    ),

  getSession: (sessionId: string) =>
    request(`/api/sessions/${sessionId}`, sessionResponseSchema).then(
      (response) => response.session,
    ),

  listSessionEvents: (sessionId: string, afterSeq = 0, limit = 50) =>
    request(
      `/api/sessions/${sessionId}/events?after_seq=${afterSeq}&limit=${limit}`,
      sessionEventsSchema,
    ),

  /** 202. Generation continues in the background; the socket carries the stream. */
  startTurn: (
    sessionId: string,
    body: {
      text: string
      attachments?: { uploadId: string; mimeType: string; filename?: string }[]
      /** The answer to a gated tool; see `ConfirmationPrompt`. */
      confirmation?: { functionCallId: string; confirmed: boolean }
    },
    idempotencyKey?: string,
  ) =>
    request(`/api/sessions/${sessionId}/turns`, turnAcceptedSchema, {
      method: 'POST',
      body: {
        text: body.text,
        // Projected down to the two fields the contract defines. `TurnAttachment` sets
        // `extra="forbid"`, so passing `filename` through — which the caller carries for
        // its optimistic echo — would be a 422.
        attachments: (body.attachments ?? []).map((attachment) => ({
          uploadId: attachment.uploadId,
          mimeType: attachment.mimeType,
        })),
        ...(body.confirmation ? { confirmation: body.confirmation } : {}),
      },
      ...(idempotencyKey ? { idempotencyKey } : {}),
    }),

  cancelTurn: (sessionId: string, turnId: string) =>
    request(
      `/api/sessions/${sessionId}/turns/${turnId}/cancel`,
      turnStatusResponseSchema,
      { method: 'POST' },
    ),

  getTurn: (turnId: string) => request(`/api/turns/${turnId}`, turnStatusResponseSchema),

  /**
   * Mint a single-use, 60-second socket ticket.
   *
   * Never cached: a ticket is consumed on connect, so a second reconnect reusing one
   * would be refused (docs/04-api-contract.md#authentication).
   */
  createWsTicket: () => request('/api/ws-ticket', wsTicketSchema, { method: 'POST' }),

  // --- uploads --------------------------------------------------------------------------

  /**
   * An attachment's bytes, as a blob.
   *
   * Fetched with the ID token and turned into an object URL rather than being pointed at
   * from an `<img src>`, because an `<img>` cannot carry a bearer header. The alternative
   * — a URL that is its own credential — would be a second way into the data, and
   * docs/00-overview.md keeps one auth path on purpose.
   */
  getEventAttachment: async (sessionId: string, seq: number, index: number) => {
    const token = await getAuthProvider().getIdToken()
    const response = await fetch(
      `/api/sessions/${sessionId}/events/${seq}/attachments/${index}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} },
    )
    if (!response.ok) throw new ApiError(response.status, {
      type: 'about:blank',
      title: response.statusText || 'Request failed',
      status: response.status,
      detail: '',
    })
    return response.blob()
  },

  createUpload: (body: { filename: string; mimeType: string; sizeBytes: number }) =>
    request('/api/uploads', uploadCreatedSchema, { method: 'POST', body }),

  finalizeUpload: (uploadId: string) =>
    request(`/api/uploads/${uploadId}/finalize`, uploadFinalizedSchema, { method: 'POST' }),
}
