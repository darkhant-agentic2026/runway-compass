/**
 * A roadmap run's final document, in full — the right pane of the research view for a
 * roadmap run (`ResearchViewPage`), `ResearchReport`'s sibling for `build_roadmap_workflow`
 * (docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer).
 *
 * The plan itself is still immutable — this is `plan_tailor`'s one write, read back exactly
 * as posted — but the tasks it proposes are not the coach's last word: `project_coach` can
 * revise a *copy* of this plan later (`materialize_study_plan` is what turns a plan into
 * board tasks, once it is wired into a caller). Rendering every proposed task as its own
 * `ProposedTaskCard`, decision chip included, is what makes a later revision legible against
 * this one — a learner comparing "what the plan said" to "what actually landed on the
 * board" needs the full list, not just the tasks that made the cut.
 */

import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';

import { Markdown } from '@/components/markdown/Markdown';
import { ProposedTaskCard } from '@/components/research/ProposedTaskCard';
import { pluralize } from '@/lib/format';
import type { StudyPlan } from '@/lib/schemas';

const SELECTED_DECISIONS = new Set(['include', 'additional']);

export function StudyPlanView({ plan }: { plan: StudyPlan }) {
  const [memoOpen, setMemoOpen] = useState(false);
  const tasksBySlug = Object.fromEntries(plan.proposedTasks.map((task) => [task.slug, task]));
  const titleBySlug = Object.fromEntries(
    plan.proposedTasks.map((task) => [task.slug, task.title]),
  );
  const includedCount = plan.plan.filter((entry) =>
    SELECTED_DECISIONS.has(entry.decision),
  ).length;
  const notIncludedCount = plan.plan.length - includedCount;

  return (
    <section
      className="space-y-4 rounded-lg border bg-card p-4"
      aria-labelledby="study-plan-heading"
      data-testid="study-plan"
    >
      <header>
        <h2 id="study-plan-heading" className="text-sm font-semibold">
          {plan.title || 'Your study plan'}
        </h2>
        {plan.shortDescription ? (
          <p className="mt-1 text-sm text-muted-foreground">{plan.shortDescription}</p>
        ) : null}
        <p className="mt-2 text-xs text-muted-foreground" data-testid="study-plan-tally">
          {pluralize(includedCount, 'task')} in your plan
          {notIncludedCount > 0
            ? ` · ${pluralize(notIncludedCount, 'task')} your coach left out`
            : ''}
        </p>
      </header>

      {plan.longDescription ? (
        <div className="text-sm">
          <Markdown text={plan.longDescription} />
        </div>
      ) : null}

      <ul className="space-y-2" data-testid="proposed-task-list">
        {plan.plan.map((entry) => {
          const task = tasksBySlug[entry.taskSlug];
          if (!task) return null;
          return (
            <ProposedTaskCard
              key={entry.taskSlug}
              task={task}
              entry={entry}
              titleBySlug={titleBySlug}
            />
          );
        })}
      </ul>

      {plan.memo ? (
        <div className="border-t pt-3">
          <button
            type="button"
            onClick={() => setMemoOpen((current) => !current)}
            aria-expanded={memoOpen}
            className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            {memoOpen ? (
              <ChevronDown className="size-3.5" aria-hidden="true" />
            ) : (
              <ChevronRight className="size-3.5" aria-hidden="true" />
            )}
            Task composer’s memo
          </button>
          {memoOpen ? (
            <div className="mt-2 text-sm" data-testid="plan-memo">
              <Markdown text={plan.memo} />
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
