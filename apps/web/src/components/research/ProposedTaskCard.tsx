/**
 * One `task_proposer`-proposed task, tailored by `plan_tailor` into a `PlanTaskEntry` —
 * the unit `StudyPlanView` renders its list from
 * (docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer).
 *
 * Two things the card has to carry at once, since the plan document keeps them on two
 * separate objects sharing only a `slug`: `task` is what the material *is* — required and
 * optional items, same shape a `ResearchReport`'s — and `entry` is `plan_tailor`'s verdict
 * on it — `include` / `additional` / `exclude` / `reject`, plus the `why`. The decision is
 * always visible as a corner chip, and the `why` is always visible too, even collapsed —
 * it is the whole point of an `exclude`/`reject` entry, which otherwise reads as the coach
 * silently dropping something the learner asked about. Everything else — the material
 * itself — is behind a click, and rendered a little muted for a task that did not make the
 * plan, so a learner scanning the list sees what is in at a glance and can still open what
 * is not to see why.
 */

import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';

import { ItemKindBadge, ItemKindStrip } from '@/components/task/item-kind';
import { Badge } from '@/components/ui/badge';
import { formatMinutes } from '@/lib/format';
import type { PlanDecision, PlanTaskEntry, ProposedItem, ProposedTask } from '@/lib/schemas';
import { cn } from '@/lib/utils';

const DECISION_LABELS: Record<PlanDecision, string> = {
  include: 'Included',
  additional: 'Deep dive',
  exclude: 'Not included',
  reject: 'Rejected',
};

const DECISION_VARIANT: Record<
  PlanDecision,
  'default' | 'secondary' | 'outline' | 'destructive'
> = {
  include: 'default',
  additional: 'secondary',
  exclude: 'outline',
  reject: 'destructive',
};

//: The two decisions `materialize_study_plan` turns into board tasks
// (`StudyPlanService.DEFAULT_MATERIALIZE_DECISIONS`) — everything else stays muted here,
// on the same "did this become real work" line the backend already draws.
const SELECTED_DECISIONS: ReadonlySet<PlanDecision> = new Set(['include', 'additional']);

interface ProposedTaskCardProps {
  task: ProposedTask;
  entry: PlanTaskEntry;
  /** Slug → title, for rendering `after`/`prerequisiteTasks` as names rather than slugs. */
  titleBySlug: Record<string, string>;
}

export function ProposedTaskCard({ task, entry, titleBySlug }: ProposedTaskCardProps) {
  const [expanded, setExpanded] = useState(false);
  const selected = SELECTED_DECISIONS.has(entry.decision);
  const totalMinutes = task.required.reduce((sum, item) => sum + item.minutes, 0);
  const prerequisites = [
    ...(entry.after ? [entry.after] : []),
    ...entry.prerequisiteTasks,
  ].filter((slug, index, all) => all.indexOf(slug) === index);

  return (
    <li
      className={cn(
        'relative rounded-lg border bg-card p-3 transition-opacity',
        !selected && 'opacity-70 hover:opacity-100',
      )}
      data-testid="proposed-task"
      data-decision={entry.decision}
    >
      <div className="absolute top-2 right-2">
        <Badge variant={DECISION_VARIANT[entry.decision]} data-testid="decision-chip">
          {DECISION_LABELS[entry.decision]}
        </Badge>
      </div>

      <button
        type="button"
        onClick={() => setExpanded((current) => !current)}
        aria-expanded={expanded}
        aria-label={expanded ? `Collapse ${task.title}` : `Expand ${task.title}`}
        className="flex w-full items-start gap-2 pr-24 text-left"
      >
        {expanded ? (
          <ChevronDown
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
        ) : (
          <ChevronRight
            className="mt-0.5 size-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{task.title}</p>
          <ItemKindStrip
            minutes={totalMinutes}
            required={task.required}
            optional={task.optional}
            className="mt-0.5"
          />
        </div>
      </button>

      {entry.why ? (
        <p className="mt-2 pl-6 text-xs text-muted-foreground" data-testid="decision-why">
          {entry.why}
        </p>
      ) : null}

      {expanded ? (
        <div className="mt-3 space-y-3 border-t pt-3 pl-6">
          {task.description ? <p className="text-sm">{task.description}</p> : null}

          {prerequisites.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
              <span>After:</span>
              {prerequisites.map((slug) => (
                <Badge key={slug} variant="outline" className="text-[0.7rem]">
                  {titleBySlug[slug] ?? slug}
                </Badge>
              ))}
            </div>
          ) : null}

          {task.required.length > 0 ? (
            <ItemList label="Required" items={task.required} testId="proposed-required" />
          ) : null}
          {task.optional.length > 0 ? (
            <ItemList label="Optional" items={task.optional} testId="proposed-optional" />
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function ItemList({
  label,
  items,
  testId,
}: {
  label: string;
  items: ProposedItem[];
  testId: string;
}) {
  return (
    <div data-testid={testId}>
      <h4 className="mb-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {label}
      </h4>
      <ul className="space-y-1.5">
        {items.map((item, index) => (
          <li key={`${item.title}-${index}`} className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <ItemKindBadge kind={item.kind} />
              <Badge variant="secondary" className="text-[0.7rem]">
                {formatMinutes(item.minutes)}
              </Badge>
            </div>
            {item.url ? (
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 block text-sm text-primary underline underline-offset-2"
              >
                {item.title}
              </a>
            ) : (
              <p className="mt-1 text-sm">{item.title}</p>
            )}
            {item.why ? <p className="text-xs text-muted-foreground">{item.why}</p> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}
