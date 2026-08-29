/**
 * A leaf task's checklist — the top block of the workspace's left pane.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid and
 * docs/02-data-model.md#task-items. When every box here is ticked and no research is
 * outstanding, the task completes itself; the checkbox is therefore the primary control on
 * this screen, not a decoration beside the state badge.
 *
 * **A guided item must never render its `details`.** For an unguided item the details are
 * the instruction — read this page, watch that video — and rendering them is the whole
 * point. For a guided one they are the coach's teaching notes, and the exercise's answer
 * is in there. The distinction is enforced here, asserted in `Checklist.test.tsx`, and
 * stated in docs/06-frontend.md so a future screen does not rediscover it the hard way.
 *
 * **The link is not part of that rule.** `url` is the item's *source*, not the coach's
 * notes — the same reasoning `ResearchReport.tsx` and `ProposedTaskCard.tsx` apply to the
 * same field — so it renders for a guided item exactly as it does for an unguided one.
 */

import { useLayoutEffect, useRef, useState } from 'react';

import { ItemKindBadge } from '@/components/task/item-kind';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { formatMinutes } from '@/lib/format';
import type { TaskItem } from '@/lib/schemas';
import { cn } from '@/lib/utils';

interface ChecklistProps {
  items: TaskItem[];
  /** Sum of the required items' minutes, from the report that produced them. */
  budgetMinutes: number | null;
  onToggle: (itemId: string, completed: boolean) => void;
  disabled?: boolean;
  /**
   * Unique per rendered checklist. The workspace shows the task's own list *and* one per
   * subtask, so a hard-coded heading id would repeat — and `aria-labelledby` pointing at a
   * duplicated id names whichever came first, which is the wrong section for every
   * checklist but one.
   */
  headingId?: string;
  heading?: string;
  /**
   * Whether the task (or subtask) this checklist belongs to is `in_progress` — the first
   * uncompleted item then gets a "you are here" marker
   * (docs/09-roadmap.md#task-board-and-task-view-polish). Omitted wherever the caller has
   * no task state to give it.
   */
  inProgress?: boolean;
}

export function Checklist({
  items,
  budgetMinutes,
  onToggle,
  disabled,
  headingId = 'checklist-heading',
  heading = 'To complete this task',
  inProgress = false,
}: ChecklistProps) {
  const firstIncompleteId = inProgress
    ? items.find((item) => !item.completed)?.itemId
    : undefined;

  /*
   * The "you are here" rule slides from the previous item to the next rather than jumping
   * (docs/09-roadmap.md#task-board-and-task-view-polish): one absolutely-positioned bar,
   * measured against whichever `<li>` is current and moved there with a CSS transition on
   * `top`/`height`, instead of a border toggled per item. `useLayoutEffect` so the move is
   * measured before paint and never flashes at the old item's position first.
   *
   * Declared ahead of the `items.length === 0` early return below — hooks must run on every
   * render regardless of what the component ends up rendering.
   */
  const listRef = useRef<HTMLDivElement | null>(null);
  const itemRefs = useRef(new Map<string, HTMLLIElement>());
  const [linePos, setLinePos] = useState<{ top: number; height: number } | null>(null);

  useLayoutEffect(() => {
    const container = listRef.current;
    const activeEl = firstIncompleteId ? itemRefs.current.get(firstIncompleteId) : undefined;
    if (!container || !activeEl) {
      setLinePos(null);
      return;
    }
    function measure() {
      if (!container || !activeEl) return;
      const containerRect = container.getBoundingClientRect();
      const itemRect = activeEl.getBoundingClientRect();
      setLinePos({ top: itemRect.top - containerRect.top, height: itemRect.height });
    }
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [firstIncompleteId, items]);

  if (items.length === 0) return null;

  const done = items.filter((item) => item.completed).length;
  const planned = items.reduce((total, item) => total + (item.minutes ?? 0), 0);

  return (
    <section
      className="rounded-lg border bg-card p-4"
      aria-labelledby={headingId}
      data-testid="checklist"
    >
      <header className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={headingId} className="text-sm font-semibold">
          {heading}
        </h2>
        {/*
          The budget meter sums the *checklist*, never the optional block — that separation
          is a product requirement and gets its own regression test (docs/08-testing.md).
        */}
        <p className="text-xs text-muted-foreground" data-testid="checklist-budget">
          {done} of {items.length} done
          {planned > 0
            ? ` · ${formatMinutes(planned)}${
                budgetMinutes ? ` of ${formatMinutes(budgetMinutes)}` : ''
              }`
            : ''}
        </p>
      </header>

      <div ref={listRef} className="relative" data-testid="checklist-items">
        {/*
          The sliding rule itself — solid, in the "in progress" blue. A dashed edge was
          tried first and looked ragged rather than deliberate. `pointer-events-none` since
          it overlaps the items it points at.
        */}
        <div
          aria-hidden="true"
          data-testid="you-are-here-line"
          className={cn(
            'pointer-events-none absolute left-0 w-1 rounded-full bg-progress-fill transition-[top,height] duration-300 ease-out',
            linePos ? 'opacity-100' : 'opacity-0',
          )}
          style={linePos ? { top: linePos.top, height: linePos.height } : undefined}
        />
        <ol className="space-y-3 pl-3">
          {items.map((item) => {
            const isHere = item.itemId === firstIncompleteId;
            return (
              <li
                key={item.itemId}
                ref={(el) => {
                  if (el) itemRefs.current.set(item.itemId, el);
                  else itemRefs.current.delete(item.itemId);
                }}
                className="flex gap-3"
                data-testid={isHere ? 'you-are-here' : undefined}
              >
                {isHere ? <span className="sr-only">You are here: </span> : null}
                <Checkbox
                  id={`${headingId}-${item.itemId}`}
                  checked={item.completed}
                  disabled={disabled}
                  onCheckedChange={(checked) => onToggle(item.itemId, checked === true)}
                  aria-label={item.shortDescription}
                  className="mt-0.5 shrink-0"
                />
                <div className="min-w-0 flex-1 space-y-1">
                  <label
                    htmlFor={`${headingId}-${item.itemId}`}
                    className={
                      item.completed
                        ? 'block text-sm text-muted-foreground line-through'
                        : 'block text-sm'
                    }
                  >
                    {item.shortDescription}
                  </label>

                  <div className="flex flex-wrap items-center gap-2">
                    {item.kind ? <ItemKindBadge kind={item.kind} /> : null}
                    {item.minutes ? (
                      <Badge variant="secondary" className="text-[0.7rem]">
                        {formatMinutes(item.minutes)}
                      </Badge>
                    ) : null}
                    {item.guided ? (
                      <Badge variant="outline" className="text-[0.7rem]">
                        with your coach
                      </Badge>
                    ) : null}
                  </div>

                  {/*
                Unguided only. See the module docstring: a guided item's details are the
                coach's notes and showing them hands over the answer.
              */}
                  {!item.guided && item.details ? (
                    <p
                      className="text-xs whitespace-pre-wrap text-muted-foreground"
                      data-testid="item-details"
                    >
                      {item.details}
                    </p>
                  ) : null}
                  {item.url ? (
                    <a
                      href={item.url}
                      target="_blank"
                      // `noreferrer` as well as `noopener`: these links come from pages the
                      // coach fetched, and the referrer would name the learner's own app.
                      rel="noopener noreferrer"
                      className="inline-block text-xs text-primary underline underline-offset-2"
                    >
                      Open
                    </a>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
