/**
 * The research report, in full — summary, required and optional material, citations.
 *
 * Since M8 this renders on its own screen
 * (docs/06-frontend.md#research-view-projectsprojectidresearchrunid), not inline in the
 * task workspace. Before M8, `required[]` was omitted here because it was already visible
 * as the task's `Checklist`; the research view is a different screen with no checklist of
 * its own next to it — a project-scoped report has nowhere else `required[]` could ever
 * appear — so this renders both lists.
 *
 * **Optional items have no checkbox, and that is structural rather than conventional.**
 * `required[]` is rendered read-only here too — ticking a step happens on the task's own
 * `Checklist`, from `task.items`, a different document than `report.required`, so there is
 * no shared component that could be given a checkbox by accident.
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
}

export function ResearchReport({ report, onFeedback }: ResearchReportProps) {
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

      {report.required.length > 0 ? (
        <div data-testid="report-required">
          <h3 className="mb-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {report.taskId ? 'Required for this task' : 'What answers the question'}
          </h3>
          <ul className="space-y-2">
            {report.required.map((item) => (
              <li key={item.itemId} className="min-w-0">
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
                {item.why ? <p className="text-xs text-muted-foreground">{item.why}</p> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

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
