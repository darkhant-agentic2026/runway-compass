/**
 * A board row.
 *
 * docs/06-frontend.md:
 *
 * > Card shows: title, estimated duration chip, state badge, research status (a small
 * > "materials ready" indicator), and `origin: agent` badge when the coach created it.
 * > **Parent cards show `rollup.subtaskCount` and `rollup.totalEstimatedMinutes`**
 * > ("4 subtasks · 2 h 30 m") with a progress ring for `completedSubtasks`. Expanding
 * > reveals subtasks inline.
 */

import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, ChevronRight, GripVertical, Sparkles } from 'lucide-react';
import type { CSSProperties } from 'react';

import { ProgressRing } from '@/components/board/ProgressRing';
import { StateLabel } from '@/components/board/StateLabel';
import {
  AnimatedText,
  chipRevealSequence,
  titleRevealMs,
} from '@/components/board/task-reveal';
import { TaskRowActions } from '@/components/board/TaskRowActions';
import { ItemKindStrip } from '@/components/task/item-kind';
import { Badge } from '@/components/ui/badge';
import { formatMinutes, pluralize } from '@/lib/format';
import type { Task, TaskState, TaskWithSubtasks } from '@/lib/schemas';
import { cn } from '@/lib/utils';

interface TaskCardProps {
  task: TaskWithSubtasks;
  isNextUp: boolean;
  collapsed: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  dragDisabled: boolean;
  onToggleCollapsed: () => void;
  onSetState: (taskId: string, state: TaskState, postponedUntil?: string) => void;
  onMove: (taskId: string, direction: -1 | 1) => void;
  /**
   * Whether this card just appeared and should play its entrance animation — fade in, then
   * type its title letter by letter, then fade in its content pieces one at a time
   * (docs/09-roadmap.md#task-board-and-task-view-polish). `BoardPage` sets this only for a
   * task not seen in a previous render, never on the board's first load.
   */
  isNew?: boolean;
  /** When `isNew`, how long to wait before this card starts appearing — `BoardPage` staggers
   * a batch of new cards so each one starts once the previous card's title finishes. */
  revealDelayMs?: number;
}

export function TaskCard({
  task,
  isNextUp,
  collapsed,
  canMoveUp,
  canMoveDown,
  dragDisabled,
  onToggleCollapsed,
  onSetState,
  onMove,
  isNew = false,
  revealDelayMs = 0,
}: TaskCardProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: dragDisabled,
  });

  const hasSubtasks = task.subtasks.length > 0;
  const reveal = isNew
    ? { cardDelayMs: revealDelayMs, chipDelayMs: revealDelayMs + titleRevealMs(task.title) }
    : null;

  /*
   * One content piece at a time, once the title finishes — badges, then duration, then
   * state, then research status, then progress, then item kinds
   * (docs/09-roadmap.md#task-board-and-task-view-polish). `nextChip()` is called at most
   * once per piece, only for a piece that will actually render, in the order it appears on
   * the card — a card missing a piece (no rollup, no research status) doesn't leave a gap in
   * the sequence for the pieces after it.
   */
  type ChipRevealProps = { className?: string; style?: CSSProperties };
  const EMPTY_REVEAL: ChipRevealProps = {};
  const nextChip = reveal ? chipRevealSequence(reveal.chipDelayMs) : () => EMPTY_REVEAL;

  const hasRollup = Boolean(task.rollup && task.rollup.subtaskCount > 0);
  const hasItemProgress = !hasRollup && task.items.length > 0;
  const hasResearchStatus =
    task.researchStatus === 'done' ||
    task.researchStatus === 'pending' ||
    task.researchStatus === 'in_progress';

  const nextUpReveal = isNextUp ? nextChip() : EMPTY_REVEAL;
  const agentReveal = task.origin === 'agent' ? nextChip() : EMPTY_REVEAL;
  const estimateReveal = nextChip();
  const stateReveal = nextChip();
  const researchStatusReveal = hasResearchStatus ? nextChip() : EMPTY_REVEAL;
  const progressReveal = hasRollup || hasItemProgress ? nextChip() : EMPTY_REVEAL;
  const itemKindReveal = hasItemProgress ? nextChip() : EMPTY_REVEAL;

  return (
    <li
      ref={setNodeRef}
      style={{
        transform: CSS.Translate.toString(transform),
        transition,
        ...(reveal ? { animationDelay: `${reveal.cardDelayMs}ms` } : {}),
      }}
      className={cn(
        'rounded-lg border bg-card',
        isDragging && 'z-10 opacity-70 shadow-lg',
        isNextUp && 'ring-2 ring-progress-fill/60',
        reveal && 'animate-task-card-in',
      )}
      data-testid="task-card"
      data-task-id={task.id}
      data-state={task.state}
    >
      <div className="flex items-start gap-2 p-3">
        <button
          type="button"
          className="mt-0.5 cursor-grab text-muted-foreground hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          aria-label={`Reorder ${task.title}`}
          disabled={dragDisabled}
          {...attributes}
          {...listeners}
        >
          <GripVertical className="size-4" aria-hidden="true" />
        </button>

        {hasSubtasks ? (
          <button
            type="button"
            onClick={onToggleCollapsed}
            aria-expanded={!collapsed}
            aria-label={collapsed ? `Expand ${task.title}` : `Collapse ${task.title}`}
            className="mt-0.5 text-muted-foreground hover:text-foreground"
          >
            {collapsed ? (
              <ChevronRight className="size-4" aria-hidden="true" />
            ) : (
              <ChevronDown className="size-4" aria-hidden="true" />
            )}
          </button>
        ) : (
          <span className="w-4" aria-hidden="true" />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {/*
              A plain anchor rather than react-router's `<Link>`: this component is
              rendered by tests that mount it without a router, and a `<Link>` throws
              outside one. The board is a full page load away from the workspace either
              way, so client-side navigation buys nothing here.
            */}
            <a
              href={`/projects/${task.projectId}/tasks/${task.id}`}
              className="truncate font-medium hover:underline"
              data-testid="open-workspace"
            >
              {reveal ? (
                <AnimatedText text={task.title} startDelayMs={reveal.cardDelayMs} />
              ) : (
                task.title
              )}
            </a>
            {isNextUp ? <Badge {...nextUpReveal}>Next up</Badge> : null}
            {task.origin === 'agent' ? (
              <Badge
                variant="secondary"
                {...agentReveal}
                className={cn(
                  'bg-agent-badge text-agent-badge-foreground',
                  agentReveal.className,
                )}
              >
                <Sparkles className="size-3" aria-hidden="true" />
                From your coach
              </Badge>
            ) : null}
          </div>

          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span data-testid="estimate" {...estimateReveal}>
              {formatMinutes(task.estimatedMinutes)}
            </span>
            {/*
              The separator travels with whatever it introduces, so a piece and its "· "
              fade in together rather than the dot appearing on its own beat.
            */}
            <span
              className="inline-flex items-center gap-2"
              data-testid="state-chip"
              {...stateReveal}
            >
              <span aria-hidden="true">·</span>
              <StateLabel state={task.state} />
            </span>
            {task.researchStatus === 'done' ? (
              <span
                className="inline-flex items-center gap-2"
                data-testid="research-status-chip"
                {...researchStatusReveal}
              >
                <span aria-hidden="true">·</span>
                <span data-testid="materials-ready">Materials ready</span>
              </span>
            ) : null}
            {/*
              Research the learner queued by hand, waiting for the next tick
              (docs/06-frontend.md#task-board-projectsprojectid). In the same slot as
              "Materials ready" because they are two points on one lifecycle and never
              both true.

              A badge rather than a control: queueing and cancelling both live in the
              workspace, and the board is where a learner who queued several tasks wants
              to see what is waiting — not a second place to change it.
            */}
            {task.researchStatus === 'pending' ? (
              <span
                className="inline-flex items-center gap-2"
                data-testid="research-status-chip"
                {...researchStatusReveal}
              >
                <span aria-hidden="true">·</span>
                <span data-testid="research-queued">Starts soon</span>
              </span>
            ) : null}
            {task.researchStatus === 'in_progress' ? (
              <span
                className="inline-flex items-center gap-2"
                data-testid="research-status-chip"
                {...researchStatusReveal}
              >
                <span aria-hidden="true">·</span>
                <span data-testid="research-running">Your coach is preparing this</span>
              </span>
            ) : null}
          </div>

          {hasRollup && task.rollup ? (
            <div
              className={cn('mt-2 flex items-center gap-2', progressReveal.className)}
              style={progressReveal.style}
              data-testid="rollup"
            >
              <ProgressRing
                completed={task.rollup.completedSubtasks}
                total={task.rollup.subtaskCount}
              />
              <span className="text-sm text-muted-foreground">
                {pluralize(task.rollup.subtaskCount, 'subtask')} ·{' '}
                {formatMinutes(task.rollup.totalEstimatedMinutes)}
              </span>
            </div>
          ) : hasItemProgress ? (
            /*
              A leaf's checklist progress, in the same slot a parent's rollup occupies.
              They are the same idea — how far through its plan this task is — and they
              never both apply: `items` and `rollup` are mutually exclusive
              (docs/02-data-model.md#task-items), which is why this is an `else if`.
            */
            <>
              <div
                className={cn('mt-2 flex items-center gap-2', progressReveal.className)}
                style={progressReveal.style}
                data-testid="item-progress"
              >
                <ProgressRing
                  completed={task.items.filter((item) => item.completed).length}
                  total={task.items.length}
                />
                <span className="text-sm text-muted-foreground">
                  {task.items.filter((item) => item.completed).length} of {task.items.length}{' '}
                  done
                </span>
              </div>
              {/* Icons only — `estimate` above already carries the task's duration, so
                  repeating it here would be the same number twice on one card. */}
              <ItemKindStrip
                required={task.items}
                className={cn('mt-1', itemKindReveal.className)}
                style={itemKindReveal.style}
              />
            </>
          ) : null}
        </div>

        <TaskRowActions
          task={task}
          canMoveUp={canMoveUp}
          canMoveDown={canMoveDown}
          onSetState={(state, postponedUntil) => onSetState(task.id, state, postponedUntil)}
          onMove={(direction) => onMove(task.id, direction)}
        />
      </div>

      {hasSubtasks && !collapsed ? (
        <ul className="border-t" data-testid="subtask-list">
          {task.subtasks.map((subtask) => (
            <SubtaskRow
              key={subtask.id}
              subtask={subtask}
              onSetState={(state, postponedUntil) =>
                onSetState(subtask.id, state, postponedUntil)
              }
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function SubtaskRow({
  subtask,
  onSetState,
}: {
  subtask: Task;
  onSetState: (state: TaskState, postponedUntil?: string) => void;
}) {
  return (
    <li
      className="flex items-center gap-2 py-2 pr-3 pl-12 text-sm"
      data-testid="subtask"
      data-task-id={subtask.id}
      data-state={subtask.state}
    >
      <span className={cn('flex-1 truncate', subtask.state === 'completed' && 'line-through')}>
        {subtask.title}
      </span>
      <span className="text-muted-foreground">{formatMinutes(subtask.estimatedMinutes)}</span>
      {/* Subtasks are ordered within their parent, so they get no move actions here —
          reordering them is a drag inside the expanded list, which lands with the
          workspace at M2. */}
      <TaskRowActions
        task={subtask}
        canMoveUp={false}
        canMoveDown={false}
        onSetState={onSetState}
        onMove={() => {}}
      />
    </li>
  );
}
