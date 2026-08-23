/**
 * Task board — `/projects/:projectId`.
 *
 * docs/06-frontend.md#task-board. Drag-and-drop is dnd-kit, optimistic, and computes the
 * new fractional index with the same algorithm the server uses, so the row lands where it
 * will finally sit and does not jump when the response arrives.
 *
 * **The intake conversation lives here, from M3.** docs/04-api-contract.md has
 * `POST /api/projects` open a session with `taskId: null`, and docs/06-frontend.md gives
 * that session no screen of its own — so it belongs on the one screen a new project lands
 * on. Beside the board rather than on a route of its own, which is also what makes golden
 * flow #1 legible: the learner watches tasks appear as the coach proposes them, which is
 * what the `board_update` push is for.
 */

import {
  closestCenter,
  DndContext,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  sortableKeyboardCoordinates,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';

import { BoardFilters } from '@/components/board/BoardFilters';
import { CoachUpdateBanner } from '@/components/board/CoachUpdateBanner';
import { TaskCard } from '@/components/board/TaskCard';
import { ResearchCard } from '@/components/research/ResearchCard';
import { StartProjectResearch } from '@/components/research/StartProjectResearch';
import { SessionPane } from '@/components/session/SessionPane';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  useBoard,
  useCreateTask,
  useEffectivePrefs,
  useProject,
  useProjectRuns,
  useProjectSession,
  useReorderTask,
  useSetTaskState,
  useUndoRun,
} from '@/features/queries';
import { formatMinutes } from '@/lib/format';
import type { AutonomousRun, TaskState } from '@/lib/schemas';
import { useBoardUiStore } from '@/stores/boardUi';

/**
 * A frozen module-level constant, never `?? []` inline.
 *
 * `runs.data` is undefined until the query settles, and a fresh literal in its place is a
 * new value on every render — which React reports as minified error #185, in a built
 * bundle only (docs/06-frontend.md, `NO_ATTACHMENTS` in `stores/composer.ts`).
 */
const EMPTY_RUNS: AutonomousRun[] = [];

export default function BoardPage() {
  const { projectId = '' } = useParams();
  const filters = useBoardUiStore((state) => state.filtersFor(projectId));
  const toggleFilter = useBoardUiStore((state) => state.toggleFilter);
  const isCollapsed = useBoardUiStore((state) => state.isCollapsed);
  const toggleCollapsed = useBoardUiStore((state) => state.toggleCollapsed);

  const project = useProject(projectId);
  const prefs = useEffectivePrefs(projectId);
  const intake = useProjectSession(projectId);
  const board = useBoard(projectId, filters);
  const setTaskState = useSetTaskState(projectId, filters);
  const reorder = useReorderTask(projectId, filters);
  const createTask = useCreateTask(projectId);
  const runs = useProjectRuns(projectId);
  const undoRun = useUndoRun(projectId);

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const tasks = board.data ?? [];
  const nextUpTaskId = project.data?.nextUpTaskId ?? null;

  function move(taskId: string, targetIndex: number) {
    const others = tasks.filter((task) => task.id !== taskId);
    const clamped = Math.max(0, Math.min(targetIndex, others.length));
    const anchor =
      clamped === 0
        ? { beforeTaskId: others[0]?.id }
        : { afterTaskId: others[clamped - 1]?.id };
    if (!anchor.beforeTaskId && !anchor.afterTaskId) return;
    reorder.mutate({ taskId, targetIndex: clamped, ...anchor });
  }

  function onDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const from = tasks.findIndex((task) => task.id === active.id);
    const to = tasks.findIndex((task) => task.id === over.id);
    if (from < 0 || to < 0) return;
    // dnd-kit reports the index in the *full* list; `move` positions relative to the
    // list without the dragged row, which is the same convention the server uses.
    move(String(active.id), from < to ? to : to);
  }

  if (project.isError) {
    return <p className="p-6 text-muted-foreground">That project could not be loaded.</p>;
  }

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6 lg:flex-row lg:items-start">
      <div className="w-full space-y-6 lg:w-3/5">
        <header className="space-y-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h1 className="text-2xl font-semibold">{project.data?.title ?? 'Board'}</h1>
            <Button variant="outline" render={<Link to={`/projects/${projectId}/settings`} />}>
              Project settings
            </Button>
          </div>
          {project.data?.goal ? (
            <p className="text-muted-foreground">{project.data.goal}</p>
          ) : null}
          {project.data ? (
            <p className="text-sm text-muted-foreground">
              {project.data.counts.completed} of {project.data.counts.total} done ·{' '}
              {formatMinutes(project.data.counts.openMinutes)} of open work
              {prefs.data
                ? ` · default task ${formatMinutes(prefs.data.defaultTaskMinutes)}`
                : ''}
            </p>
          ) : null}
        </header>

        {/*
          What the coach did while the learner was away, above the filters and below the
          project's own numbers — the first thing to read on a board that moved on its own.
          It renders nothing when no run changed anything, which is the common case.
        */}
        <CoachUpdateBanner
          runs={runs.data ?? EMPTY_RUNS}
          tasks={tasks}
          undoing={undoRun.isPending}
          onUndo={(runId) => undoRun.mutate(runId)}
        />

        {/*
          The project's most recent research job, if it has one — added at M8
          (docs/06-frontend.md#task-board-projectsprojectid). `runs` is already fetched
          for the banner above; `[0]` is "newest first" from the same query.
        */}
        <ResearchCard projectId={projectId} run={runs.data?.[0]} />

        <StartProjectResearch
          projectId={projectId}
          intakeSessionId={intake.data?.id ?? project.data?.intakeSessionId ?? null}
        />

        <BoardFilters
          filters={filters}
          onToggle={(filter) => toggleFilter(projectId, filter)}
        />

        <AddTaskForm
          defaultMinutes={prefs.data?.defaultTaskMinutes ?? 45}
          pending={createTask.isPending}
          onAdd={(title, estimatedMinutes) => createTask.mutate({ title, estimatedMinutes })}
        />

        {board.isPending ? (
          <p className="text-muted-foreground">Loading the board…</p>
        ) : tasks.length === 0 ? (
          <p className="rounded-lg border border-dashed p-6 text-center text-muted-foreground">
            No tasks yet. Add the first one above.
          </p>
        ) : (
          <DndContext
            sensors={sensors}
            collisionDetection={closestCenter}
            onDragEnd={onDragEnd}
          >
            <SortableContext
              items={tasks.map((task) => task.id)}
              strategy={verticalListSortingStrategy}
            >
              <ul className="space-y-2" data-testid="board">
                {tasks.map((task, index) => (
                  <TaskCard
                    key={task.id}
                    task={task}
                    isNextUp={task.id === nextUpTaskId}
                    collapsed={isCollapsed(task.id)}
                    canMoveUp={index > 0}
                    canMoveDown={index < tasks.length - 1}
                    // From M5 the board is read-only while an autonomous run holds the
                    // project lease; until then nothing can hold it.
                    dragDisabled={false}
                    onToggleCollapsed={() => toggleCollapsed(task.id)}
                    onSetState={(taskId, state: TaskState, postponedUntil) =>
                      setTaskState.mutate({
                        taskId,
                        state,
                        ...(postponedUntil ? { postponedUntil } : {}),
                      })
                    }
                    onMove={(taskId, direction) => move(taskId, index + direction)}
                  />
                ))}
              </ul>
            </SortableContext>
          </DndContext>
        )}
      </div>

      <SessionPane
        sessionId={intake.data?.id ?? project.data?.intakeSessionId ?? ''}
        projectId={projectId}
        heading="Planning conversation with your coach"
        emptyHint="Tell your coach what you want to learn and how much time you have. It will ask a few questions before proposing tasks."
        className="relative flex h-[70svh] w-full flex-col rounded-lg border lg:sticky lg:top-4 lg:h-[calc(100vh-8rem)] lg:w-2/5"
      />
    </div>
  );
}

function AddTaskForm({
  defaultMinutes,
  pending,
  onAdd,
}: {
  defaultMinutes: number;
  pending: boolean;
  onAdd: (title: string, estimatedMinutes: number) => void;
}) {
  const [title, setTitle] = useState('');
  const [minutes, setMinutes] = useState(String(defaultMinutes));

  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = title.trim();
        if (!trimmed) return;
        onAdd(trimmed, Number(minutes) || defaultMinutes);
        setTitle('');
        setMinutes(String(defaultMinutes));
      }}
    >
      <div className="min-w-48 flex-1">
        <Label htmlFor="new-task-title">New task</Label>
        <Input
          id="new-task-title"
          value={title}
          placeholder="What comes next?"
          onChange={(event) => setTitle(event.target.value)}
        />
      </div>
      <div className="w-28">
        <Label htmlFor="new-task-minutes">Minutes</Label>
        <Input
          id="new-task-minutes"
          type="number"
          min={1}
          max={1440}
          value={minutes}
          onChange={(event) => setMinutes(event.target.value)}
        />
      </div>
      <Button type="submit" disabled={pending || title.trim().length === 0}>
        Add task
      </Button>
    </form>
  );
}
