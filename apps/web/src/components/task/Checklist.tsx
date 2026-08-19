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
 */

import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { formatMinutes } from '@/lib/format';
import type { TaskItem } from '@/lib/schemas';

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
}

export function Checklist({
  items,
  budgetMinutes,
  onToggle,
  disabled,
  headingId = 'checklist-heading',
  heading = 'To complete this task',
}: ChecklistProps) {
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

      <ol className="space-y-3">
        {items.map((item) => (
          <li key={item.itemId} className="flex gap-3">
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
              {!item.guided && item.url ? (
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
        ))}
      </ol>
    </section>
  );
}
