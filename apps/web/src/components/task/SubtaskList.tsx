/**
 * A composite task's subtasks, on the task's own screen.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid:
 *
 * > **A composite task shows its subtasks as cards**, between the detail and the research
 * > report. `GET /api/tasks/{id}` already returns `subtasks[]`, so this costs no request.
 * > **A subtask has no route of its own, and that is the product decision rather than a
 * > gap.** The parent's session is where subtasks are worked through […] So the cards are
 * > a checklist inside this screen […] Nothing here navigates.
 *
 * Which is why no card is a link and there is no `open-workspace` anchor: the board's
 * `TaskCard` has one because a *parent* has a workspace, and a subtask deliberately does
 * not. Everything else — the legal-transitions menu, the state labels, the ring — is the
 * board's own component, so the two screens cannot drift into offering different actions
 * for the same row.
 *
 * **And each card carries its own checklist**, because "no route of its own" has to mean
 * "reachable from here" rather than "unreachable". A subtask holds items exactly as a leaf
 * task does — the first one inherits the parent's — so without this the coach could plan
 * work the learner had no way to see, let alone tick off. `GET /api/tasks/{id}` already
 * returns `subtasks[]` with their items, so this still costs no extra request.
 */

import { useState } from 'react';

import { ProgressRing } from '@/components/board/ProgressRing';
import { STATE_LABELS, transitionsFor } from '@/components/board/task-state';
import { TaskRowActions } from '@/components/board/TaskRowActions';
import { Checklist } from '@/components/task/Checklist';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { formatMinutes, pluralize } from '@/lib/format';
import type { Rollup, Task, TaskState } from '@/lib/schemas';
import { cn } from '@/lib/utils';

interface SubtaskListProps {
  subtasks: Task[];
  /** The parent's rollup, which is what the board's parent card shows. */
  rollup: Rollup | null;
  onSetState: (taskId: string, state: TaskState, postponedUntil?: string) => void;
  /** Tick or un-tick one of a subtask's checklist items. */
  onToggleItem: (taskId: string, itemId: string, completed: boolean) => void;
  itemsDisabled?: boolean;
  /** Create one subtask under this task. Sized by the project's own default — there is no
   * per-subtask duration input, the same reasoning `estimatedMinutes` no longer needs one
   * once a checklist exists to size it (docs/09-roadmap.md#task-board-and-task-view-polish). */
  onAdd: (title: string) => void;
  addDisabled?: boolean;
  /**
   * Rendered even with no subtasks, because adding the first one is how a task *becomes*
   * composite — and that write moves the parent's checklist onto the new child
   * ([02-data-model.md](02-data-model.md#task-items)), which is worth warning about.
   */
  hasItems: boolean;
}

export function SubtaskList({
  subtasks,
  rollup,
  onSetState,
  onToggleItem,
  itemsDisabled,
  onAdd,
  addDisabled,
  hasItems,
}: SubtaskListProps) {
  return (
    // `subtask-cards`, not `subtask-list`: the board's expanded parent already owns that
    // name, and a selector that matches on two screens is a test that passes on the wrong
    // one.
    <section aria-labelledby="subtasks-heading" data-testid="subtask-cards">
      <div className="flex items-center gap-2">
        <h2 id="subtasks-heading" className="text-sm font-medium">
          Subtasks
        </h2>
        {rollup && rollup.subtaskCount > 0 ? (
          <>
            <ProgressRing
              completed={rollup.completedSubtasks}
              total={rollup.subtaskCount}
              size={20}
            />
            {/*
              Straight from `rollup`, the field the board's parent card reads, rather than
              recounted from the rows below. Two screens deriving the same number two ways
              is how they end up disagreeing — and the server excludes discarded subtasks
              from all three of these (services/rollups.py), which is why a discarded row
              can be on screen without being in the count.
            */}
            <span className="text-sm text-muted-foreground" data-testid="subtask-rollup">
              {rollup.completedSubtasks} of {pluralize(rollup.subtaskCount, 'subtask')} done ·{' '}
              {formatMinutes(rollup.totalEstimatedMinutes)}
            </span>
          </>
        ) : null}
      </div>

      {subtasks.length === 0 ? (
        <p className="mt-1 text-sm text-muted-foreground">
          {hasItems
            ? 'Adding a subtask moves this task’s steps onto it.'
            : 'Break this into subtasks if it is really more than one sitting.'}
        </p>
      ) : null}

      <AddSubtask onAdd={onAdd} disabled={addDisabled ?? false} />

      <ul className="mt-2 space-y-2">
        {subtasks.map((subtask) => (
          <SubtaskCard
            key={subtask.id}
            subtask={subtask}
            onSetState={(state, postponedUntil) =>
              onSetState(subtask.id, state, postponedUntil)
            }
            onToggleItem={(itemId, completed) => onToggleItem(subtask.id, itemId, completed)}
            itemsDisabled={itemsDisabled ?? false}
          />
        ))}
      </ul>
    </section>
  );
}

function AddSubtask({
  onAdd,
  disabled,
}: {
  onAdd: (title: string) => void;
  disabled: boolean;
}) {
  const [title, setTitle] = useState('');

  /*
    The hand path to a subtask, and the only one since `POST /api/tasks/{id}/split` was
    removed. It lives here rather than on the board's row menu because this is the screen a
    composite task is worked from — and because "Split into subtasks…" produced a whole
    breakdown from nothing, which is exactly the shape that made the tool unusable.

    No minutes field: a subtask is sized from the project's own default, the same as any
    other task created without an explicit override, and its displayed duration follows
    its checklist once one exists (docs/09-roadmap.md#task-board-and-task-view-polish).
  */
  return (
    <form
      className="mt-2 flex flex-wrap items-end gap-2"
      data-testid="add-subtask"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = title.trim();
        if (!trimmed) return;
        onAdd(trimmed);
        setTitle('');
      }}
    >
      <div className="min-w-40 flex-1 space-y-1">
        <Label htmlFor="new-subtask-title" className="text-xs text-muted-foreground">
          New subtask
        </Label>
        <Input
          id="new-subtask-title"
          value={title}
          disabled={disabled}
          maxLength={300}
          onChange={(event) => setTitle(event.target.value)}
        />
      </div>
      <Button type="submit" size="sm" variant="outline" disabled={disabled || !title.trim()}>
        Add subtask
      </Button>
    </form>
  );
}

function SubtaskCard({
  subtask,
  onSetState,
  onToggleItem,
  itemsDisabled,
}: {
  subtask: Task;
  onSetState: (state: TaskState, postponedUntil?: string) => void;
  onToggleItem: (itemId: string, completed: boolean) => void;
  itemsDisabled: boolean;
}) {
  // The one obvious next step, promoted out of the menu: "Start" on a fresh subtask,
  // "Complete" on the one being worked on. Taken from `transitionsFor` rather than
  // written out, so it is legal by construction and cannot produce a 409 — notably,
  // `not_started` → `completed` is *not* a transition, so a bare completion checkbox
  // would be an error for every subtask nobody has started yet.
  const [primary] = transitionsFor(subtask.state);
  const quickAction = primary && !primary.needsDate ? primary : null;
  const dimmed = subtask.state === 'discarded';

  return (
    <li
      className={cn('rounded-lg border bg-card p-3', dimmed && 'opacity-60')}
      data-testid="subtask-card"
      data-task-id={subtask.id}
      data-state={subtask.state}
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p
            className={cn(
              'font-medium',
              (subtask.state === 'completed' || dimmed) && 'line-through',
            )}
          >
            {subtask.title}
          </p>
          {subtask.description ? (
            <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
              {subtask.description}
            </p>
          ) : null}
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <span data-testid="subtask-estimate">
              {formatMinutes(subtask.estimatedMinutes)}
            </span>
            <span aria-hidden="true">·</span>
            <span data-testid="subtask-state">{STATE_LABELS[subtask.state]}</span>
            {subtask.origin === 'agent' ? (
              <Badge variant="secondary" className="bg-agent-badge text-agent-badge-foreground">
                From your coach
              </Badge>
            ) : null}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-1">
          {quickAction ? (
            <Button variant="outline" size="sm" onClick={() => onSetState(quickAction.target)}>
              {quickAction.label}
            </Button>
          ) : null}
          {/*
            Subtasks are ordered within their parent and this screen has no drag surface,
            so both move directions are disabled — the board's expanded list is where
            reordering happens.
          */}
          <TaskRowActions
            task={subtask}
            canMoveUp={false}
            canMoveDown={false}
            onSetState={onSetState}
            onMove={() => {}}
          />
        </div>
      </div>

      {/*
        The subtask's own checklist, inline. No budget meter: the meter compares a
        checklist against the *report* that produced it, and a subtask's report is fetched
        for the task being viewed rather than for each child — so a meter here would either
        need a request per subtask or would quietly show the parent's number.
      */}
      {subtask.items.length > 0 ? (
        <div className="mt-3 border-t pt-3">
          <Checklist
            items={subtask.items}
            budgetMinutes={null}
            disabled={itemsDisabled}
            onToggle={onToggleItem}
            headingId={`subtask-checklist-${subtask.id}`}
            heading="Steps for this subtask"
            inProgress={subtask.state === 'in_progress'}
          />
        </div>
      ) : null}
    </li>
  );
}
