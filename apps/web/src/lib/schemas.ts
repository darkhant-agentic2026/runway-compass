/**
 * Zod schemas for everything crossing the network boundary.
 *
 * docs/06-frontend.md validates WebSocket frames with Zod at the boundary; the same
 * discipline is applied to REST responses here, so a server-side shape change surfaces as
 * a named parse error rather than as `undefined` three components deep.
 *
 * These mirror `apps/api/src/coach/services/models.py`.
 */

import { z } from 'zod';

export const taskStateSchema = z.enum([
  'draft',
  'not_started',
  'in_progress',
  'completed',
  'postponed',
  'postponed_until',
  'discarded',
]);
export type TaskState = z.infer<typeof taskStateSchema>;

export const researchStatusSchema = z.enum([
  'none',
  'pending',
  'in_progress',
  'done',
  'failed',
]);
export const originSchema = z.enum(['user', 'agent']);
export const projectStatusSchema = z.enum(['active', 'paused', 'archived']);
export const guidanceStyleSchema = z.enum(['socratic', 'direct', 'mixed']);
export const verbositySchema = z.enum(['terse', 'balanced', 'thorough']);
export const researchDepthSchema = z.enum(['light', 'standard', 'deep']);

export const rollupSchema = z.object({
  subtaskCount: z.number().int(),
  completedSubtasks: z.number().int(),
  totalEstimatedMinutes: z.number().int(),
});
export type Rollup = z.infer<typeof rollupSchema>;

/**
 * One entry on a leaf task's checklist (docs/02-data-model.md#task-items).
 *
 * `details` is asymmetric between guided and unguided, and the UI has to respect that:
 * for an unguided item it is the instruction and is rendered; for a guided one it is the
 * coach's teaching notes, the exercise's answer lives in there, and it must **not** be
 * rendered (docs/06-frontend.md).
 */
export const taskItemSchema = z.object({
  itemId: z.string(),
  shortDescription: z.string(),
  details: z.string().default(''),
  guided: z.boolean().default(false),
  completed: z.boolean().default(false),
  completedAt: z.string().nullable().default(null),
  minutes: z.number().int().nullable().default(null),
  url: z.string().nullable().default(null),
  sourceReportId: z.string().nullable().default(null),
});
export type TaskItem = z.infer<typeof taskItemSchema>;

export const taskSchema = z.object({
  id: z.string(),
  projectId: z.string(),
  ownerUid: z.string(),
  parentTaskId: z.string().nullable().default(null),
  title: z.string(),
  description: z.string().default(''),
  state: taskStateSchema,
  postponedUntil: z.string().nullable().default(null),
  estimatedMinutes: z.number().int(),
  actualMinutes: z.number().int().nullable().default(null),
  order: z.string(),
  sessionId: z.string().nullable().default(null),
  needsResearch: z.boolean().default(true),
  researchStatus: researchStatusSchema.default('none'),
  latestReportId: z.string().nullable().default(null),
  /** Leaf tasks only; mutually exclusive with `rollup`. */
  items: z.array(taskItemSchema).default([]),
  rollup: rollupSchema.nullable().default(null),
  origin: originSchema.default('user'),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
  completedAt: z.string().nullable().default(null),
});
export type Task = z.infer<typeof taskSchema>;

export const taskWithSubtasksSchema = taskSchema.extend({
  subtasks: z.array(taskSchema).default([]),
});
export type TaskWithSubtasks = z.infer<typeof taskWithSubtasksSchema>;

export const projectPrefsSchema = z.object({
  defaultTaskMinutes: z.number().int().nullable().default(null),
  guidanceStyle: guidanceStyleSchema.nullable().default(null),
  researchDepth: researchDepthSchema.nullable().default(null),
  allowVideos: z.boolean().nullable().default(null),
  confirmItemCompletion: z.boolean().nullable().default(null),
  preferredSources: z.array(z.string()).nullable().default(null),
  avoidSources: z.array(z.string()).nullable().default(null),
});
export type ProjectPrefs = z.infer<typeof projectPrefsSchema>;

export const projectCountsSchema = z.object({
  total: z.number().int(),
  completed: z.number().int(),
  openMinutes: z.number().int(),
});

export const projectSchema = z.object({
  id: z.string(),
  ownerUid: z.string(),
  title: z.string(),
  goal: z.string().default(''),
  status: projectStatusSchema,
  prefs: projectPrefsSchema,
  nextUpTaskId: z.string().nullable().default(null),
  /**
   * The intake session `POST /api/projects` opens (docs/04-api-contract.md). A pointer on
   * the project so that the board can render the conversation without a round trip to
   * resolve it; `POST /api/projects/{id}/session` is the fallback for projects created
   * before it existed.
   */
  intakeSessionId: z.string().nullable().default(null),
  counts: projectCountsSchema,
  lastAutonomousRunAt: z.string().nullable().default(null),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
});
export type Project = z.infer<typeof projectSchema>;

export const effectivePrefsSchema = z.object({
  defaultTaskMinutes: z.number().int(),
  guidanceStyle: guidanceStyleSchema,
  verbosity: verbositySchema,
  timezone: z.string(),
  researchDepth: researchDepthSchema,
  allowVideos: z.boolean(),
  confirmItemCompletion: z.boolean(),
  preferredSources: z.array(z.string()),
  avoidSources: z.array(z.string()),
});
export type EffectivePrefs = z.infer<typeof effectivePrefsSchema>;

export const globalPrefsSchema = z.object({
  defaultTaskMinutes: z.number().int(),
  guidanceStyle: guidanceStyleSchema,
  verbosity: verbositySchema,
  timezone: z.string(),
  autonomousEnabled: z.boolean(),
  autonomousQuietHours: z.object({ start: z.string(), end: z.string() }),
});
export type GlobalPrefs = z.infer<typeof globalPrefsSchema>;

export const learnerProfileSchema = z.object({
  thinkingStyle: z.string().default(''),
  strengths: z.array(z.string()).default([]),
  gaps: z.array(z.string()).default([]),
  technologies: z
    .array(z.object({ name: z.string(), level: z.string(), evidence: z.string().default('') }))
    .default([]),
  pacing: z.string().default(''),
  feedbackNotes: z.array(z.string()).default([]),
  updatedAt: z.string().nullable().default(null),
  updatedBy: z.enum(['agent', 'user']).default('user'),
  version: z.number().int().default(0),
});
export type LearnerProfile = z.infer<typeof learnerProfileSchema>;

export const meSchema = z.object({
  uid: z.string(),
  email: z.string().nullable(),
  displayName: z.string().nullable(),
  photoUrl: z.string().nullable(),
  globalPrefs: globalPrefsSchema,
  learnerProfile: learnerProfileSchema,
  plan: z.object({
    tier: z.string(),
    limits: z.object({ autonomousRunsPerDay: z.number().int() }),
  }),
});
export type Me = z.infer<typeof meSchema>;

export const projectListSchema = z.object({ projects: z.array(projectSchema) });
export const boardSchema = z.object({ tasks: z.array(taskWithSubtasksSchema) });
export const effectivePrefsResponseSchema = z.object({
  projectId: z.string(),
  effectivePrefs: effectivePrefsSchema,
});

export const projectDerivedSchema = z.object({
  id: z.string(),
  nextUpTaskId: z.string().nullable(),
  counts: z.record(z.string(), z.number()),
});

export const taskMutationSchema = z.object({
  task: taskSchema,
  parent: taskSchema.nullable().default(null),
  project: projectDerivedSchema.nullable().default(null),
});
export type TaskMutation = z.infer<typeof taskMutationSchema>;

// --- research reports ------------------------------------------------------------------

export const reportItemKindSchema = z.enum([
  'article',
  'video',
  'exercise',
  'doc',
  'code_scaffold',
]);
export type ReportItemKind = z.infer<typeof reportItemKindSchema>;

export const reportItemSchema = z.object({
  itemId: z.string(),
  kind: reportItemKindSchema,
  title: z.string(),
  url: z.string().nullable().default(null),
  minutes: z.number().int(),
  why: z.string().default(''),
  details: z.string().default(''),
  source: z.enum(['youtube', 'web', 'generated']).default('web'),
  meta: z.record(z.string(), z.string()).default({}),
  guided: z.boolean().nullable().default(null),
});
export type ReportItem = z.infer<typeof reportItemSchema>;

/**
 * `progress` holds feedback and nothing else. Per-item completion moved onto the task at
 * M4 (docs/02-data-model.md#task-items): a task has one checklist and may have several
 * reports, so completion on the report was answering the wrong question.
 */
export const researchReportSchema = z.object({
  id: z.string(),
  projectId: z.string(),
  ownerUid: z.string(),
  taskId: z.string(),
  runId: z.string().nullable().default(null),
  sessionId: z.string().nullable().default(null),
  summary: z.string().default(''),
  required: z.array(reportItemSchema).default([]),
  optional: z.array(reportItemSchema).default([]),
  totalRequiredMinutes: z.number().int().default(0),
  budgetMinutes: z.number().int().default(45),
  citations: z.array(z.object({ uri: z.string(), title: z.string().default('') })).default([]),
  progress: z
    .object({ feedback: z.record(z.string(), z.enum(['up', 'down'])).default({}) })
    .default({ feedback: {} }),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
});
export type ResearchReport = z.infer<typeof researchReportSchema>;

export const reportListSchema = z.object({ reports: z.array(researchReportSchema) });
export const reportResponseSchema = z.object({ report: researchReportSchema });

export const researchAcceptedSchema = z.object({
  runId: z.string(),
  turnId: z.string().nullable(),
  mode: z.string(),
});

export const taskDetailSchema = z.object({
  task: taskWithSubtasksSchema,
  latestReport: researchReportSchema.nullable().default(null),
});
export type TaskDetail = z.infer<typeof taskDetailSchema>;

// --- sessions & turns ------------------------------------------------------------------

export const sessionSummarySchema = z.object({
  id: z.string(),
  projectId: z.string().nullable().default(null),
  taskId: z.string().nullable().default(null),
});
export type SessionSummary = z.infer<typeof sessionSummarySchema>;

export const sessionResponseSchema = z.object({ session: sessionSummarySchema });

/**
 * One transcript row. `event` is the serialized ADK `Event` verbatim — the API returns it
 * unprojected on purpose (docs/02-data-model.md), so the parts this UI actually renders
 * are picked out by `transcript.ts` rather than pinned by a schema that would have to
 * track a pinned dependency's model.
 */
export const sessionEventSchema = z.object({
  seq: z.number().int(),
  eventId: z.string(),
  event: z.record(z.string(), z.unknown()),
});
export type SessionEvent = z.infer<typeof sessionEventSchema>;

export const sessionEventsSchema = z.object({
  events: z.array(sessionEventSchema),
  nextAfterSeq: z.number().int(),
  hasMore: z.boolean(),
});

export const turnStatusSchema = z.enum(['running', 'complete', 'failed', 'cancelled']);
export type TurnStatus = z.infer<typeof turnStatusSchema>;

export const turnAcceptedSchema = z.object({
  turnId: z.string(),
  sessionId: z.string(),
  status: turnStatusSchema,
  startSeq: z.number().int().default(0),
});

export const turnStatusResponseSchema = z.object({
  turnId: z.string(),
  status: turnStatusSchema,
  lastSeq: z.number().int(),
});

export const wsTicketSchema = z.object({ ticket: z.string(), expiresAt: z.string() });

export const uploadCreatedSchema = z.object({ uploadId: z.string(), signedUrl: z.string() });
export const uploadFinalizedSchema = z.object({ uploadId: z.string(), mimeType: z.string() });

/** RFC 9457 problem+json, which is how every API error arrives. */
export const problemSchema = z.object({
  type: z.string().default('about:blank'),
  title: z.string().default('Error'),
  status: z.number().int(),
  detail: z.string().default(''),
  instance: z.string().optional(),
  /**
   * Present on the `409` from `POST /api/sessions/{sid}/research`: the run already holding
   * the project's agent lease. What makes that response actionable rather than a dead end
   * (docs/04-api-contract.md#post-apisessionssidresearch).
   */
  runId: z.string().optional(),
});
export type Problem = z.infer<typeof problemSchema>;
