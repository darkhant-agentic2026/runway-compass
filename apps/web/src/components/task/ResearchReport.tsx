/**
 * The research report — everything the run found that is *not* the checklist.
 *
 * docs/06-frontend.md#task-workspace-projectsprojectidtaskstaskid. The required items moved
 * onto the task at M4 and render as `Checklist`; what is left here is the summary, the
 * optional material, and the citations.
 *
 * **Optional items have no checkbox, and that is now structural rather than conventional.**
 * The two blocks read from different documents — the checklist from `task.items`, this from
 * `report.optional` — so there is no shared component that could be given a checkbox by
 * accident. Asserted anyway, because it is a product requirement (docs/08-testing.md).
 */

import { ThumbsDown, ThumbsUp } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { formatMinutes } from '@/lib/format';
import type { ResearchReport as Report, ReportItem } from '@/lib/schemas';

const KIND_LABELS: Record<ReportItem['kind'], string> = {
  article: 'Article',
  video: 'Video',
  exercise: 'Exercise',
  doc: 'Docs',
  code_scaffold: 'Scaffold',
};

interface ResearchReportProps {
  report: Report;
  onFeedback?: (itemId: string, feedback: 'up' | 'down' | null) => void;
  /** Earlier runs, rendered collapsed. docs/10-risks.md Q4: reports accumulate. */
  earlier?: Report[];
}

export function ResearchReport({ report, onFeedback, earlier = [] }: ResearchReportProps) {
  return (
    <section
      className="space-y-4 rounded-lg border bg-card p-4"
      aria-labelledby="report-heading"
      data-testid="research-report"
    >
      <header>
        <h2 id="report-heading" className="text-sm font-semibold">
          What your coach found
        </h2>
        {report.summary ? (
          <p className="mt-1 text-sm text-muted-foreground">{report.summary}</p>
        ) : null}
      </header>

      {report.optional.length > 0 ? (
        <div data-testid="report-optional">
          <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Optional, if you want to go deeper
          </h3>
          <ul className="space-y-2">
            {report.optional.map((item) => (
              <li key={item.itemId} className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline" className="text-[0.7rem]">
                      {KIND_LABELS[item.kind]}
                    </Badge>
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
                  {item.why ? (
                    <p className="text-xs text-muted-foreground">{item.why}</p>
                  ) : null}
                </div>
                {onFeedback ? (
                  <Feedback
                    current={report.progress.feedback[item.itemId] ?? null}
                    onChange={(next) => onFeedback(item.itemId, next)}
                  />
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.citations.length > 0 ? (
        <div>
          <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
            Sources
          </h3>
          <ul className="space-y-1">
            {report.citations.map((citation) => (
              <li key={citation.uri} className="truncate text-xs">
                <a
                  href={citation.uri}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-muted-foreground underline underline-offset-2"
                >
                  {citation.title || citation.uri}
                </a>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/*
        Q4 (docs/10-risks.md): reports accumulate, newest shown by default, older ones
        collapsible. A `<details>` rather than component state — it is a disclosure, and
        the browser already has one that is keyboard-accessible and survives a re-render.
      */}
      {earlier.length > 0 ? (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            {earlier.length} earlier {earlier.length === 1 ? 'run' : 'runs'}
          </summary>
          <ul className="mt-2 space-y-2">
            {earlier.map((old) => (
              <li key={old.id} className="rounded border p-2">
                <p className="text-muted-foreground">{old.summary || 'No summary'}</p>
                <p className="mt-1 text-[0.7rem] text-muted-foreground">
                  {old.required.length} required · {old.optional.length} optional ·{' '}
                  {formatMinutes(old.totalRequiredMinutes)}
                </p>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function Feedback({
  current,
  onChange,
}: {
  current: 'up' | 'down' | null;
  onChange: (next: 'up' | 'down' | null) => void;
}) {
  return (
    <div className="flex shrink-0 gap-1">
      <Button
        variant={current === 'up' ? 'secondary' : 'ghost'}
        size="icon"
        aria-label="This was useful"
        aria-pressed={current === 'up'}
        onClick={() => onChange(current === 'up' ? null : 'up')}
      >
        <ThumbsUp className="size-3.5" />
      </Button>
      <Button
        variant={current === 'down' ? 'secondary' : 'ghost'}
        size="icon"
        aria-label="This was not useful"
        aria-pressed={current === 'down'}
        onClick={() => onChange(current === 'down' ? null : 'down')}
      >
        <ThumbsDown className="size-3.5" />
      </Button>
    </div>
  );
}
