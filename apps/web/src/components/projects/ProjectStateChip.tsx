/**
 * A project's state, as a chip with a tooltip — shared between the project list, the
 * archived-projects view, and the board header (`ProjectsPage.tsx`, `ArchivedProjectsPage.tsx`,
 * `BoardPage.tsx`). docs/09-roadmap.md#project-state-and-archiving.
 */

import {
  PROJECT_STATE_ACCENT,
  PROJECT_STATE_GUARD_HINT,
  PROJECT_STATE_LABELS,
  type ProjectStatus,
} from '@/components/projects/project-state';
import { Badge } from '@/components/ui/badge';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import { cn } from '@/lib/utils';

export function ProjectStateChip({ status }: { status: ProjectStatus }) {
  const accent = PROJECT_STATE_ACCENT[status];
  const Icon = accent.icon;
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Badge variant="outline" className={cn('gap-1', accent.className)} />}
        data-testid="project-state-chip"
        data-status={status}
      >
        <Icon className="size-3" aria-hidden="true" />
        {PROJECT_STATE_LABELS[status]}
      </TooltipTrigger>
      <TooltipContent>{PROJECT_STATE_GUARD_HINT[status]}</TooltipContent>
    </Tooltip>
  );
}
