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
export type GuidanceStyle = z.infer<typeof guidanceStyleSchema>;

export const guidanceLevelSchema = z.enum(['mostly_guided', 'balanced', 'mostly_unguided']);
export type GuidanceLevel = z.infer<typeof guidanceLevelSchema>;

export const verbositySchema = z.enum(['terse', 'balanced', 'thorough']);
export type Verbosity = z.infer<typeof verbositySchema>;

export const researchDepthSchema = z.enum(['light', 'standard', 'deep']);
export type ResearchDepth = z.infer<typeof researchDepthSchema>;

// Moved above `taskItemSchema`, which now reuses it: `kind` was dropped when a report/
// proposed item was promoted into a checklist item, until this UI rework threaded it
// through end to end (`apps/api/src/coach/services/models.py`'s `TaskItem.kind`) so a
// checklist row can show the same kind chip its source item did.
export const reportItemKindSchema = z.enum([
  'article',
  'video',
  'exercise',
  'doc',
  'code_scaffold',
]);
export type ReportItemKind = z.infer<typeof reportItemKindSchema>;

export const taskItemSchema = z.object({
  itemId: z.string(),
  shortDescription: z.string(),
  details: z.string().default(''),
  //: `null` for a hand-added item, and for any item added before this field existed.
  kind: reportItemKindSchema.nullable().default(null),
  guided: z.boolean().default(false),
  completed: z.boolean().default(false),
  completedAt: z.string().nullable().default(null),
  minutes: z.number().int().nullable().default(null),
  url: z.string().nullable().default(null),
  sourceReportId: z.string().nullable().default(null),
});
export type TaskItem = z.infer<typeof taskItemSchema>;

export const rollupSchema = z.object({
  subtaskCount: z.number().int(),
  completedSubtasks: z.number().int(),
  totalEstimatedMinutes: z.number().int(),
});
export type Rollup = z.infer<typeof rollupSchema>;

export const taskSchema = z.object({
  id: z.string(),
  projectId: z.string(),
  ownerUid: z.string(),
  parentTaskId: z.string().nullable().default(null),
  title: z.string(),
  description: z.string().default(''),
  state: taskStateSchema,
  postponedUntil: z.string().nullable().default(null),
  estimatedMinutes: z.number().int().default(45),
  actualMinutes: z.number().int().nullable().default(null),
  order: z.string(),
  sessionId: z.string().nullable().default(null),
  needsResearch: z.boolean().default(true),
  researchStatus: z.enum(['none', 'pending', 'in_progress', 'done', 'failed']).default('none'),
  researchRequestedAt: z.string().nullable().default(null),
  latestReportId: z.string().nullable().default(null),
  studyPlanRunId: z.string().nullable().default(null),
  items: z.array(taskItemSchema).default([]),
  rollup: rollupSchema.nullable().default(null),
  origin: z.enum(['user', 'agent']).default('user'),
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
  guidanceLevel: guidanceLevelSchema.nullable().default(null),
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

export const roadmapBriefSchema = z.object({
  subject: z.string().default(''),
  specificTopics: z.array(z.string()).default([]),
  timeBudget: z.string().default(''),
  additionalNotes: z.string().default(''),
  attachments: z.array(z.string()).default([]),
  updatedAt: z.string().nullable().default(null),
});
export type RoadmapBrief = z.infer<typeof roadmapBriefSchema>;

export const projectSchema = z.object({
  id: z.string(),
  ownerUid: z.string(),
  title: z.string(),
  description: z.string().default(''),
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
  roadmapBrief: roadmapBriefSchema.nullable().default(null),
  counts: projectCountsSchema,
  lastAutonomousRunAt: z.string().nullable().default(null),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
});
export type Project = z.infer<typeof projectSchema>;

/** `POST /api/projects/{id}/troubleshooting/delete-all-tasks` — project settings'
 * troubleshooting action, not a board mutation. */
export const deleteAllTasksResponseSchema = z.object({
  deletedTasks: z.number().int(),
  resetPlans: z.number().int(),
});

export const effectivePrefsSchema = z.object({
  defaultTaskMinutes: z.number().int(),
  guidanceStyle: guidanceStyleSchema,
  guidanceLevel: guidanceLevelSchema.default('balanced'),
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
  skills: z
    .array(
      z.object({
        name: z.string(),
        area: z.string().default('general'),
        level: z.string(),
        evidence: z.string().default(''),
      }),
    )
    .default([]),
  pacing: z.string().default(''),
  feedbackNotes: z.array(z.string()).default([]),
  updatedAt: z.string().nullable().default(null),
  updatedBy: z.enum(['agent', 'user']).default('user'),
  version: z.number().int().default(0),
});
export type LearnerProfile = z.infer<typeof learnerProfileSchema>;

export const learnerProfileResponseSchema = z.object({
  learnerProfile: learnerProfileSchema,
});

export const planLimitsSchema = z.object({
  autonomousRunsPerDay: z.number().int(),
  monthlyPoints: z.number().int(),
  fourHourPoints: z.number().int(),
});
export type PlanLimits = z.infer<typeof planLimitsSchema>;

/** One usage window (`GET /api/me`'s `usage.{monthly,fourHour}`), M8-quotas. */
export const usageWindowSchema = z.object({
  spent: z.number().int(),
  limit: z.number().int(),
  resetsAt: z.string(),
});
export type UsageWindow = z.infer<typeof usageWindowSchema>;

export const usageStatusSchema = z.object({
  monthly: usageWindowSchema,
  fourHour: usageWindowSchema,
});
export type UsageStatus = z.infer<typeof usageStatusSchema>;

export const meSchema = z.object({
  uid: z.string(),
  email: z.string().nullable(),
  displayName: z.string().nullable(),
  photoUrl: z.string().nullable(),
  globalPrefs: globalPrefsSchema,
  learnerProfile: learnerProfileSchema,
  plan: z.object({
    tier: z.string(),
    limits: planLimitsSchema,
  }),
  usage: usageStatusSchema,
});
export type Me = z.infer<typeof meSchema>;

export const couponClaimResponseSchema = z.object({
  plan: z.object({ tier: z.string(), limits: planLimitsSchema }),
});
export type CouponClaimResponse = z.infer<typeof couponClaimResponseSchema>;

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
  /** `null` since M8: a report from research about the project as a whole, kicked off
   * with no parent task — nothing is promoted into any task's checklist for one. */
  taskId: z.string().nullable().default(null),
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

export const reportResponseSchema = z.object({ report: researchReportSchema });

export const researchAcceptedSchema = z.object({
  runId: z.string(),
  turnId: z.string().nullable(),
  /** + M8: the run's own dedicated session — the research view watches this one, not
   * the session the request was made from. */
  sessionId: z.string(),
  mode: z.string(),
});

export const taskDetailSchema = z.object({
  task: taskWithSubtasksSchema,
  latestReport: researchReportSchema.nullable().default(null),
});
export type TaskDetail = z.infer<typeof taskDetailSchema>;

// --- autonomous runs -------------------------------------------------------------------
// docs/05-autonomous-runs.md#run-ledger. The banner reads `changes[]` and `undoneAt`; the
// run detail reads `steps[]`. Parsed loosely on purpose where the server owns the
// vocabulary — `status` and `step.id` are strings rather than enums here, because a run
// gaining a sixth step should not make an older tab fail to parse the ledger it is
// rendering. The UI switches on the values it knows and shows the rest as-is.

export const runStepSchema = z.object({
  id: z.string(),
  status: z.string(),
  startedAt: z.string().nullable().default(null),
  endedAt: z.string().nullable().default(null),
  output: z.record(z.string(), z.unknown()).nullable().default(null),
  error: z.string().nullable().default(null),
});
export type RunStep = z.infer<typeof runStepSchema>;

export const runChangeSchema = z.object({
  kind: z.string(),
  taskId: z.string(),
  previousOrder: z.string().nullable().default(null),
});
export type RunChange = z.infer<typeof runChangeSchema>;

export const autonomousRunSchema = z.object({
  id: z.string(),
  ownerUid: z.string(),
  projectId: z.string(),
  taskId: z.string().nullable().default(null),
  trigger: z.string(),
  mode: z.string(),
  status: z.string(),
  attempts: z.number().int().default(1),
  maxAttempts: z.number().int().default(3),
  steps: z.array(runStepSchema).default([]),
  turnId: z.string().nullable().default(null),
  /** + M8: the run's own dedicated research session, never the caller's. */
  sessionId: z.string().nullable().default(null),
  changes: z.array(runChangeSchema).default([]),
  undoneAt: z.string().nullable().default(null),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
  error: z.string().nullable().default(null),
});
export type AutonomousRun = z.infer<typeof autonomousRunSchema>;

export const runResponseSchema = z.object({ run: autonomousRunSchema });
export const runListSchema = z.object({ runs: z.array(autonomousRunSchema) });
export const runUndoSchema = z.object({
  run: autonomousRunSchema,
  taskIds: z.array(z.string()).default([]),
});

// --- study plans -------------------------------------------------------------------------
// docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer.
// A roadmap run's final document, read back from `GET /api/runs/{runId}/plan` —
// `get_run_report`'s sibling for `build_roadmap_workflow`. `proposedItemSchema` mirrors
// `reportItemSchema` minus `itemId`: a proposed item has none yet, it is only minted once
// `materialize_study_plan` promotes it onto a real task.

export const proposedItemSchema = z.object({
  kind: reportItemKindSchema,
  title: z.string(),
  url: z.string().nullable().default(null),
  minutes: z.number().int(),
  why: z.string().default(''),
  details: z.string().default(''),
  source: z.enum(['youtube', 'web', 'generated']).default('web'),
  guided: z.boolean().nullable().default(null),
});
export type ProposedItem = z.infer<typeof proposedItemSchema>;

export const proposedTaskSchema = z.object({
  slug: z.string(),
  title: z.string(),
  description: z.string().default(''),
  required: z.array(proposedItemSchema).default([]),
  optional: z.array(proposedItemSchema).default([]),
  prerequisiteTasks: z.array(z.string()).default([]),
});
export type ProposedTask = z.infer<typeof proposedTaskSchema>;

export const planDecisionSchema = z.enum(['include', 'additional', 'exclude', 'reject']);
export type PlanDecision = z.infer<typeof planDecisionSchema>;

export const planTaskEntrySchema = z.object({
  taskSlug: z.string(),
  after: z.string().nullable().default(null),
  prerequisiteTasks: z.array(z.string()).default([]),
  relevance: z.number().int().min(0).max(4).default(0),
  decision: planDecisionSchema,
  why: z.string().default(''),
});
export type PlanTaskEntry = z.infer<typeof planTaskEntrySchema>;

export const studyPlanSchema = z.object({
  id: z.string(),
  projectId: z.string(),
  ownerUid: z.string(),
  runId: z.string().nullable().default(null),
  sessionId: z.string().nullable().default(null),
  title: z.string().default(''),
  shortDescription: z.string().default(''),
  longDescription: z.string().default(''),
  memo: z.string().default(''),
  proposedTasks: z.array(proposedTaskSchema).default([]),
  plan: z.array(planTaskEntrySchema).default([]),
  materializedAt: z.string().nullable().default(null),
  materializedTaskIds: z.array(z.string()).default([]),
  createdAt: z.string().nullable().default(null),
  updatedAt: z.string().nullable().default(null),
});
export type StudyPlan = z.infer<typeof studyPlanSchema>;

export const studyPlanResponseSchema = z.object({ plan: studyPlanSchema });

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
  /**
   * Present on the `429` from a quota-exceeded turn (M8-quotas): which window
   * (`"monthly" | "4-hour"`) and when it resets, in addition to `detail`'s
   * human-readable sentence carrying the same information
   * (docs/04-api-contract.md#usage-quotas-implemented-m8-quotas).
   */
  window: z.string().optional(),
  resetAt: z.string().optional(),
});
export type Problem = z.infer<typeof problemSchema>;
