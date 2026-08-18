import type { Project, Task, TaskWithSubtasks } from '@/lib/schemas';

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
    state: 'not_started',
    postponedUntil: null,
    estimatedMinutes: 45,
    actualMinutes: null,
    order: `a${counter}`,
    sessionId: null,
    needsResearch: true,
    researchStatus: 'none',
    latestReportId: null,
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
    goal: '',
    status: 'active',
    prefs: {
      defaultTaskMinutes: null,
      guidanceStyle: null,
      researchDepth: null,
      allowVideos: null,
      preferredSources: null,
      avoidSources: null,
    },
    nextUpTaskId: null,
    intakeSessionId: 's_intake',
    counts: { total: 0, completed: 0, openMinutes: 0 },
    lastAutonomousRunAt: null,
    createdAt: null,
    updatedAt: null,
    ...overrides,
  };
}
