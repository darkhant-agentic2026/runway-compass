import type {
  LearnerProfile,
  PlanTaskEntry,
  Project,
  ProposedItem,
  ProposedTask,
  ReportItem,
  ResearchReport,
  StudyPlan,
  Task,
  TaskItem,
  TaskWithSubtasks,
  UsageStatus,
  UsageWindow,
} from '@/lib/schemas';

let counter = 0;

export function makeTask(overrides: Partial<Task> = {}): Task {
  counter += 1;
  return {
    id: `k_${counter}`,
    projectId: 'p_1',
    ownerUid: 'u_alice',
    parentTaskId: null,
    title: `Task ${counter}`,
    description: '',
    state: 'draft',
    postponedUntil: null,
    estimatedMinutes: 45,
    actualMinutes: null,
    order: `a${counter}`,
    sessionId: null,
    needsResearch: true,
    researchStatus: 'none',
    researchRequestedAt: null,
    latestReportId: null,
    items: [],
    rollup: null,
    origin: 'user',
    createdAt: null,
    updatedAt: null,
    completedAt: null,
    ...overrides,
  };
}

export function makeParent(
  overrides: Partial<TaskWithSubtasks> = {},
  subtasks: Task[] = [],
): TaskWithSubtasks {
  const rollupSource = subtasks.filter((task) => task.state !== 'discarded');
  return {
    ...makeTask(overrides as Partial<Task>),
    subtasks,
    rollup:
      subtasks.length > 0
        ? {
            subtaskCount: rollupSource.length,
            completedSubtasks: rollupSource.filter((task) => task.state === 'completed').length,
            totalEstimatedMinutes: rollupSource.reduce(
              (total, task) => total + task.estimatedMinutes,
              0,
            ),
          }
        : null,
    ...overrides,
  };
}

export function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p_1',
    ownerUid: 'u_alice',
    title: 'Learn structured concurrency',
    description: '',
    status: 'active',
    prefs: {
      defaultTaskMinutes: null,
      guidanceStyle: null,
      guidanceLevel: null,
      researchDepth: null,
      allowVideos: null,
      confirmItemCompletion: null,
      preferredSources: null,
      avoidSources: null,
    },
    nextUpTaskId: null,
    intakeSessionId: 's_intake',
    roadmapBrief: null,
    counts: { total: 0, completed: 0, openMinutes: 0 },
    lastAutonomousRunAt: null,
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}

let itemCounter = 0;

export function makeTaskItem(overrides: Partial<TaskItem> = {}): TaskItem {
  itemCounter += 1;
  return {
    itemId: `i_${itemCounter}`,
    shortDescription: `Step ${itemCounter}`,
    details: '',
    kind: null,
    guided: false,
    completed: false,
    completedAt: null,
    minutes: null,
    url: null,
    sourceReportId: 'rep_1',
    ...overrides,
  };
}

let reportItemCounter = 0;

export function makeReportItem(overrides: Partial<ReportItem> = {}): ReportItem {
  reportItemCounter += 1;
  return {
    itemId: `ri_${reportItemCounter}`,
    kind: 'article',
    title: `Material ${reportItemCounter}`,
    url: 'https://example.com/a',
    minutes: 15,
    why: 'so you can do the exercise',
    details: '',
    source: 'web',
    meta: {},
    guided: null,
    ...overrides,
  };
}

export function makeReport(overrides: Partial<ResearchReport> = {}): ResearchReport {
  return {
    id: 'rep_1',
    projectId: 'p_1',
    ownerUid: 'u_alice',
    taskId: 'k_1',
    runId: 'r_1',
    sessionId: 's_1',
    summary: 'What I found.',
    required: [],
    optional: [],
    totalRequiredMinutes: 0,
    budgetMinutes: 45,
    citations: [],
    progress: { feedback: {} },
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}

let proposedItemCounter = 0;

export function makeProposedItem(overrides: Partial<ProposedItem> = {}): ProposedItem {
  proposedItemCounter += 1;
  return {
    kind: 'article',
    title: `Proposed material ${proposedItemCounter}`,
    url: 'https://example.com/a',
    minutes: 15,
    why: 'so you can do the exercise',
    details: '',
    source: 'web',
    guided: null,
    ...overrides,
  };
}

let proposedTaskCounter = 0;

export function makeProposedTask(overrides: Partial<ProposedTask> = {}): ProposedTask {
  proposedTaskCounter += 1;
  return {
    slug: `task-${proposedTaskCounter}`,
    title: `Proposed task ${proposedTaskCounter}`,
    description: '',
    required: [],
    optional: [],
    prerequisiteTasks: [],
    ...overrides,
  };
}

export function makePlanTaskEntry(overrides: Partial<PlanTaskEntry> = {}): PlanTaskEntry {
  return {
    taskSlug: 'task-1',
    after: null,
    prerequisiteTasks: [],
    relevance: 4,
    decision: 'include',
    why: 'This is core to your goal.',
    ...overrides,
  };
}

export function makeStudyPlan(overrides: Partial<StudyPlan> = {}): StudyPlan {
  return {
    id: 'plan_1',
    projectId: 'p_1',
    ownerUid: 'u_alice',
    runId: 'r_1',
    sessionId: 's_1',
    title: 'A study plan',
    shortDescription: 'What you need to learn.',
    longDescription: '',
    memo: '',
    proposedTasks: [],
    plan: [],
    materializedAt: null,
    materializedTaskIds: [],
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}

export function makeLearnerProfile(overrides: Partial<LearnerProfile> = {}): LearnerProfile {
  return {
    thinkingStyle: '',
    strengths: [],
    gaps: [],
    skills: [],
    pacing: '',
    feedbackNotes: [],
    updatedAt: null,
    updatedBy: 'user',
    version: 0,
    ...overrides,
  };
}

function makeUsageWindow(overrides: Partial<UsageWindow> = {}): UsageWindow {
  return { spent: 0, limit: 200, resetsAt: '2026-08-25T00:00:00+00:00', ...overrides };
}

export function makeUsageStatus(overrides: Partial<UsageStatus> = {}): UsageStatus {
  return {
    monthly: makeUsageWindow({ limit: 500 }),
    fourHour: makeUsageWindow({ limit: 80 }),
    ...overrides,
  };
}
