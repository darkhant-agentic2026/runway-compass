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

import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ChevronDown, ChevronRight, GripVertical, Sparkles } from 'lucide-react'

import { ProgressRing } from '@/components/board/ProgressRing'
import { TaskRowActions } from '@/components/board/TaskRowActions'
import { STATE_LABELS } from '@/components/board/task-state'
import { Badge } from '@/components/ui/badge'
import { formatMinutes, pluralize } from '@/lib/format'
import type { TaskState, TaskWithSubtasks, Task } from '@/lib/schemas'
import { cn } from '@/lib/utils'

interface TaskCardProps {
  task: TaskWithSubtasks
  isNextUp: boolean
  collapsed: boolean
  canMoveUp: boolean
  canMoveDown: boolean
  dragDisabled: boolean
  onToggleCollapsed: () => void
  onSetState: (taskId: string, state: TaskState, postponedUntil?: string) => void
  onMove: (taskId: string, direction: -1 | 1) => void
  onSplit: (taskId: string) => void
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
  onSplit,
}: TaskCardProps) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: task.id,
    disabled: dragDisabled,
  })

  const hasSubtasks = task.subtasks.length > 0

  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      className={cn(
        'bg-card rounded-lg border',
        isDragging && 'z-10 opacity-70 shadow-lg',
        isNextUp && 'ring-progress-fill/60 ring-2',
      )}
      data-testid="task-card"
      data-task-id={task.id}
      data-state={task.state}
    >
      <div className="flex items-start gap-2 p-3">
        <button
          type="button"
          className="text-muted-foreground hover:text-foreground mt-0.5 cursor-grab disabled:cursor-not-allowed disabled:opacity-40"
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
            className="text-muted-foreground hover:text-foreground mt-0.5"
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
            <span className="truncate font-medium">{task.title}</span>
            {isNextUp ? <Badge>Next up</Badge> : null}
            {task.origin === 'agent' ? (
              <Badge
                variant="secondary"
                className="bg-agent-badge text-agent-badge-foreground"
              >
                <Sparkles className="size-3" aria-hidden="true" />
                From your coach
              </Badge>
            ) : null}
          </div>

          <div className="text-muted-foreground mt-1 flex flex-wrap items-center gap-2 text-sm">
            <span data-testid="estimate">{formatMinutes(task.estimatedMinutes)}</span>
            <span aria-hidden="true">·</span>
            <span data-testid="state-badge">{STATE_LABELS[task.state]}</span>
            {task.researchStatus === 'done' ? (
              <>
                <span aria-hidden="true">·</span>
                <span data-testid="materials-ready">Materials ready</span>
              </>
            ) : null}
          </div>

          {task.rollup && task.rollup.subtaskCount > 0 ? (
            <div className="mt-2 flex items-center gap-2" data-testid="rollup">
              <ProgressRing
                completed={task.rollup.completedSubtasks}
                total={task.rollup.subtaskCount}
              />
              <span className="text-muted-foreground text-sm">
                {pluralize(task.rollup.subtaskCount, 'subtask')} ·{' '}
                {formatMinutes(task.rollup.totalEstimatedMinutes)}
              </span>
            </div>
          ) : null}
        </div>

        <TaskRowActions
          task={task}
          canMoveUp={canMoveUp}
          canMoveDown={canMoveDown}
          onSetState={(state, postponedUntil) => onSetState(task.id, state, postponedUntil)}
          onMove={(direction) => onMove(task.id, direction)}
          onSplit={() => onSplit(task.id)}
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
  )
}

function SubtaskRow({
  subtask,
  onSetState,
}: {
  subtask: Task
  onSetState: (state: TaskState, postponedUntil?: string) => void
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
  )
}
