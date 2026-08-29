/**
 * Archived projects — `/projects/archived`.
 *
 * docs/09-roadmap.md#project-state-and-archiving: "a dedicated view for archived
 * projects." An archived project falls out of the main list entirely
 * (`ProjectsPage.tsx` only shows `active`/`paused`), so this is the only place one can
 * still be found and, from here, restored to `active`.
 */

import { Link } from 'react-router-dom';

import { ProjectStateChip } from '@/components/projects/ProjectStateChip';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { usePatchProject, useProjects } from '@/features/queries';
import { formatMinutes } from '@/lib/format';
import type { Project } from '@/lib/schemas';

export default function ArchivedProjectsPage() {
  const projects = useProjects('archived');

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <Button variant="ghost" size="sm" render={<Link to="/" />}>
        ← Back to your projects
      </Button>
      <h1 className="text-2xl font-semibold">Archived projects</h1>
      <p className="text-muted-foreground">
        Archived projects are skipped by the autonomous scheduler and hidden from your main
        list. Restore one to pick it back up.
      </p>

      {projects.isPending ? (
        <p className="text-muted-foreground">Loading archived projects…</p>
      ) : (projects.data?.length ?? 0) === 0 ? (
        <p className="rounded-lg border border-dashed p-6 text-center text-muted-foreground">
          No archived projects.
        </p>
      ) : (
        <ul className="space-y-3" data-testid="archived-project-list">
          {projects.data?.map((project) => (
            <li key={project.id}>
              <ArchivedProjectRow project={project} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ArchivedProjectRow({ project }: { project: Project }) {
  const patch = usePatchProject(project.id);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <Link className="hover:underline" to={`/projects/${project.id}`}>
              {project.title}
            </Link>
            <ProjectStateChip status={project.status} />
          </CardTitle>
          <Button
            variant="outline"
            size="sm"
            disabled={patch.isPending}
            onClick={() => patch.mutate({ status: 'active' })}
          >
            Restore
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-1 text-sm text-muted-foreground">
        {project.description ? <p className="text-foreground">{project.description}</p> : null}
        <p>
          {project.counts.completed} of {project.counts.total} tasks done ·{' '}
          {formatMinutes(project.counts.openMinutes)} of open work
        </p>
      </CardContent>
    </Card>
  );
}
