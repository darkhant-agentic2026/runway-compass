/**
 * Project list — `/`.
 *
 * docs/06-frontend.md: "cards with progress, open minutes, 'coach updated this' badge".
 * The badge lands with autonomous runs at M5; the counts are live now.
 *
 * Fetches `active` and `paused` as two separate queries, rather than one unfiltered
 * `useProjects()` (docs/09-roadmap.md#project-state-and-archiving): a `paused` project
 * stays reachable here, behind the "Show paused projects" toggle, rather than only in a
 * place a learner would have to know to look for it. `archived` gets its own view instead
 * (`ArchivedProjectsPage.tsx`) — a list a learner has deliberately set aside shouldn't
 * keep competing for space with the ones still in progress.
 *
 * **Two filtered queries, not one unfiltered one.** `ProjectRepository.list_for_owner`
 * is backed by the `ownerUid ASC, status ASC, updatedAt DESC` index
 * (docs/02-data-model.md#indexes); passing no `status` produces `ownerUid == uid ORDER BY
 * updatedAt DESC`, which skips the index's middle field and is not a usable prefix of it —
 * Firestore returns `FAILED_PRECONDITION` for a shape the composite index was never built
 * for. `status=active` and `status=paused` each match the index exactly.
 */

import { useState } from 'react';
import { Link } from 'react-router-dom';

import { ProjectStateChip } from '@/components/projects/ProjectStateChip';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { useCreateProject, useProjects } from '@/features/queries';
import { formatMinutes } from '@/lib/format';
import type { Project } from '@/lib/schemas';

export default function ProjectsPage() {
  const active = useProjects('active');
  const paused = useProjects('paused');
  const createProject = useCreateProject();
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [showPaused, setShowPaused] = useState(false);

  const activeProjects = active.data ?? [];
  const pausedProjects = paused.data ?? [];
  const hasPaused = pausedProjects.length > 0;
  const isPending = active.isPending || paused.isPending;

  // Each query is already sorted by `updatedAt` on its own; re-sort once merged so a
  // recently-touched paused project doesn't just get appended after every active one.
  const visible: Project[] = showPaused
    ? [...activeProjects, ...pausedProjects].sort((a, b) =>
        (b.updatedAt ?? '').localeCompare(a.updatedAt ?? ''),
      )
    : activeProjects;

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-2xl font-semibold">Your projects</h1>
        <Button variant="ghost" size="sm" render={<Link to="/projects/archived" />}>
          Archived projects
        </Button>
      </div>

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = title.trim();
          if (!trimmed) return;
          createProject.mutate({ title: trimmed, description: description.trim() });
          setTitle('');
          setDescription('');
        }}
      >
        <div className="min-w-40 flex-1">
          <Label htmlFor="new-project-title">New project</Label>
          <Input
            id="new-project-title"
            value={title}
            placeholder="Learn structured concurrency"
            onChange={(event) => setTitle(event.target.value)}
          />
        </div>
        <div className="min-w-40 flex-1">
          <Label htmlFor="new-project-description">Description (optional)</Label>
          <Input
            id="new-project-description"
            value={description}
            placeholder="Ship a resilient worker pool"
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
        <Button type="submit" disabled={createProject.isPending || title.trim().length === 0}>
          Create
        </Button>
      </form>

      {hasPaused ? (
        <div className="flex items-center gap-2">
          <Switch
            id="show-paused"
            checked={showPaused}
            onCheckedChange={(checked) => setShowPaused(Boolean(checked))}
          />
          <Label htmlFor="show-paused" className="text-sm font-normal">
            Show paused projects
          </Label>
        </div>
      ) : null}

      {isPending ? (
        <p className="text-muted-foreground">Loading your projects…</p>
      ) : visible.length === 0 ? (
        <p className="rounded-lg border border-dashed p-6 text-center text-muted-foreground">
          {activeProjects.length === 0 && pausedProjects.length === 0
            ? 'No projects yet. Create one above and the coach will help you break it down.'
            : !showPaused && hasPaused
              ? 'No active projects. Some of yours are paused — turn on "Show paused projects" to see them.'
              : 'No active projects.'}
        </p>
      ) : (
        <ul className="space-y-3" data-testid="project-list">
          {visible.map((project) => (
            <li key={project.id}>
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <Link className="hover:underline" to={`/projects/${project.id}`}>
                      {project.title}
                    </Link>
                    <ProjectStateChip status={project.status} />
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-1 text-sm text-muted-foreground">
                  {project.description ? (
                    <p className="text-foreground">{project.description}</p>
                  ) : null}
                  <p>
                    {project.counts.completed} of {project.counts.total} tasks done ·{' '}
                    {formatMinutes(project.counts.openMinutes)} of open work
                  </p>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
