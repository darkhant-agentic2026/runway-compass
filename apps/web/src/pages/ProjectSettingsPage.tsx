/**
 * Project preferences — `/projects/:projectId/settings`.
 *
 * docs/06-frontend.md: "Project preferences (task duration, research depth, videos)".
 *
 * Every control shows what it currently resolves to, because a project preference is only
 * meaningful next to the global one it overrides — this is the screen where the brief's
 * "45 minutes globally, 2 hours here" is actually expressed. Clearing a field returns it
 * to inheriting.
 */

import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';

import { PROJECT_STATE_LABELS, type ProjectStatus } from '@/components/projects/project-state';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import {
  useDeleteAllProjectTasks,
  useEffectivePrefs,
  useMe,
  usePatchProject,
  useProject,
} from '@/features/queries';
import { ApiError } from '@/lib/api';
import { formatMinutes } from '@/lib/format';
import type { ProjectPrefs } from '@/lib/schemas';

const DEPTHS: NonNullable<ProjectPrefs['researchDepth']>[] = ['light', 'standard', 'deep'];
const STATES: ProjectStatus[] = ['active', 'paused', 'archived'];
const STATE_EXPLAINER: Record<ProjectStatus, string> = {
  active: 'Eligible for autonomous research and task proposals.',
  paused:
    "Paused: the autonomous scheduler's presence/status guard skips this project — no automatic research or task proposals until you switch it back to active.",
  archived:
    'Archived: skipped by the autonomous scheduler, the same as paused, and hidden from your main project list. Find it again under "Archived projects" there.',
};
const GUIDANCE_LEVELS: { value: NonNullable<ProjectPrefs['guidanceLevel']>; label: string }[] =
  [
    { value: 'mostly_guided', label: 'Mostly guided — hands-on walkthroughs with coach' },
    { value: 'balanced', label: 'Balanced — mix of guided exercises & independent reading' },
    { value: 'mostly_unguided', label: 'Mostly independent — self-driven with curated links' },
  ];

/**
 * Hidden by default — off the switch at the bottom of the page — since everything in
 * here is a maintenance action rather than a preference, and `deleteAllTasks` is
 * genuinely destructive with no soft undo (`TaskService.delete_all_tasks`'s own
 * docstring: every other removal in this app is `discard_task`'s reversible state).
 * A second, explicit confirm step before the mutation actually fires, since a switch
 * one scroll away is not the same as a click the learner meant.
 */
function TroubleshootingSection({ projectId }: { projectId: string }) {
  const [confirming, setConfirming] = useState(false);
  const deleteAllTasks = useDeleteAllProjectTasks(projectId);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Troubleshooting</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1">
          <p className="text-sm font-medium">Delete all tasks</p>
          <p className="text-xs text-muted-foreground">
            Permanently deletes every task on this project&apos;s board — there is no undo. If
            any of them came from a materialized roadmap, its study plan resets too, so asking
            your coach to materialize it again rebuilds the same tasks without re-running the
            research.
          </p>
        </div>
        {confirming ? (
          <div className="flex items-center gap-2">
            <Button
              variant="destructive"
              size="sm"
              disabled={deleteAllTasks.isPending}
              data-testid="confirm-delete-all-tasks"
              onClick={() => {
                deleteAllTasks.mutate(undefined, {
                  onSuccess: (result) => {
                    setConfirming(false);
                    toast.success(
                      result.deletedTasks > 0
                        ? `Deleted ${result.deletedTasks} task${result.deletedTasks === 1 ? '' : 's'}.`
                        : 'Nothing to delete — the board was already empty.',
                    );
                  },
                  onError: (error) => {
                    setConfirming(false);
                    const detail = error instanceof ApiError ? error.problem.detail : '';
                    toast.error(detail || 'Could not delete tasks.');
                  },
                });
              }}
            >
              {deleteAllTasks.isPending ? 'Deleting…' : 'Yes, delete everything'}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={deleteAllTasks.isPending}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
          </div>
        ) : (
          <Button
            variant="destructive"
            size="sm"
            data-testid="delete-all-tasks"
            onClick={() => setConfirming(true)}
          >
            Delete all tasks
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

export default function ProjectSettingsPage() {
  const { projectId = '' } = useParams();
  const project = useProject(projectId);
  const effective = useEffectivePrefs(projectId);
  const me = useMe();
  const patch = usePatchProject(projectId);
  const [showTroubleshooting, setShowTroubleshooting] = useState(false);

  const prefs = project.data?.prefs;
  const globalMinutes = me.data?.globalPrefs.defaultTaskMinutes;

  if (!prefs) {
    return (
      <div className="mx-auto w-full max-w-2xl space-y-6 p-4 sm:p-6">
        <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
          ← Back to the board
        </Button>
        <p className="text-muted-foreground">Loading project settings…</p>
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 p-4 sm:p-6">
      <Button variant="ghost" size="sm" render={<Link to={`/projects/${projectId}`} />}>
        ← Back to the board
      </Button>
      <h1 className="text-2xl font-semibold">{project.data?.title} — settings</h1>

      <Card>
        <CardHeader>
          <CardTitle>Project details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="project-title">Title</Label>
            <Input
              id="project-title"
              defaultValue={project.data?.title ?? ''}
              onBlur={(event) => {
                const value = event.target.value.trim();
                if (value && value !== project.data?.title) patch.mutate({ title: value });
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="project-description">Description</Label>
            <Input
              id="project-description"
              defaultValue={project.data?.description ?? ''}
              onBlur={(event) => {
                const value = event.target.value.trim();
                if (value !== project.data?.description) patch.mutate({ description: value });
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="project-state">Project state</Label>
            <Select
              value={project.data?.status}
              onValueChange={(value) => {
                if (value) patch.mutate({ status: value as ProjectStatus });
              }}
            >
              <SelectTrigger id="project-state">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATES.map((state) => (
                  <SelectItem key={state} value={state}>
                    {PROJECT_STATE_LABELS[state]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-sm text-muted-foreground" data-testid="state-explainer">
              {STATE_EXPLAINER[project.data?.status ?? 'active']}
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Preferences for this project</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-1">
            <Label htmlFor="project-minutes">Default task length (minutes)</Label>
            <div className="flex items-center gap-2">
              <Input
                id="project-minutes"
                type="number"
                min={1}
                max={1440}
                placeholder={globalMinutes ? String(globalMinutes) : ''}
                defaultValue={prefs.defaultTaskMinutes ?? ''}
                onBlur={(event) => {
                  const raw = event.target.value.trim();
                  const value = raw === '' ? null : Number(raw);
                  if (value !== prefs.defaultTaskMinutes) {
                    patch.mutate({ prefs: { defaultTaskMinutes: value } });
                  }
                }}
              />
              <Button
                variant="ghost"
                onClick={() => patch.mutate({ prefs: { defaultTaskMinutes: null } })}
                disabled={prefs.defaultTaskMinutes === null}
              >
                Inherit
              </Button>
            </div>
            <p className="text-sm text-muted-foreground" data-testid="minutes-explainer">
              {prefs.defaultTaskMinutes === null
                ? `Inheriting ${globalMinutes ? formatMinutes(globalMinutes) : 'the global default'}`
                : `Overriding the global default${
                    globalMinutes ? ` of ${formatMinutes(globalMinutes)}` : ''
                  }`}
              {effective.data
                ? ` · in effect: ${formatMinutes(effective.data.defaultTaskMinutes)}`
                : ''}
            </p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="guidance-level">Guidance amount</Label>
            <Select
              value={prefs.guidanceLevel ?? effective.data?.guidanceLevel ?? 'balanced'}
              onValueChange={(value) => {
                if (value) {
                  patch.mutate({
                    prefs: { guidanceLevel: value as ProjectPrefs['guidanceLevel'] },
                  });
                }
              }}
            >
              <SelectTrigger id="guidance-level">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {GUIDANCE_LEVELS.map((g) => (
                  <SelectItem key={g.value} value={g.value}>
                    {g.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-1">
            <Label htmlFor="preferred-sources">Topics or materials to prioritize</Label>
            <Input
              id="preferred-sources"
              placeholder="e.g. Official docs, RFCs, asyncio"
              defaultValue={(prefs.preferredSources ?? []).join(', ')}
              onBlur={(event) => {
                const raw = event.target.value.trim();
                const sources = raw
                  ? raw
                      .split(',')
                      .map((s) => s.trim())
                      .filter(Boolean)
                  : null;
                patch.mutate({ prefs: { preferredSources: sources } });
              }}
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="avoid-sources">Topics or materials to skip</Label>
            <Input
              id="avoid-sources"
              placeholder="e.g. Threading, legacy Python 2"
              defaultValue={(prefs.avoidSources ?? []).join(', ')}
              onBlur={(event) => {
                const raw = event.target.value.trim();
                const sources = raw
                  ? raw
                      .split(',')
                      .map((s) => s.trim())
                      .filter(Boolean)
                  : null;
                patch.mutate({ prefs: { avoidSources: sources } });
              }}
            />
          </div>

          <div className="space-y-1">
            <Label htmlFor="research-depth">Research depth</Label>
            <Select
              value={prefs.researchDepth ?? effective.data?.researchDepth ?? 'standard'}
              onValueChange={(value) => {
                if (value) {
                  patch.mutate({
                    prefs: { researchDepth: value as ProjectPrefs['researchDepth'] },
                  });
                }
              }}
            >
              <SelectTrigger id="research-depth">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DEPTHS.map((depth) => (
                  <SelectItem key={depth} value={depth}>
                    {depth}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center gap-2">
            <Switch
              id="allow-videos"
              checked={effective.data?.allowVideos ?? true}
              onCheckedChange={(checked) =>
                patch.mutate({ prefs: { allowVideos: Boolean(checked) } })
              }
            />
            <Label htmlFor="allow-videos" className="font-normal">
              Include videos in research
            </Label>
          </div>

          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <Switch
                id="confirm-items"
                checked={effective.data?.confirmItemCompletion ?? true}
                onCheckedChange={(checked) =>
                  patch.mutate({ prefs: { confirmItemCompletion: Boolean(checked) } })
                }
              />
              <Label htmlFor="confirm-items" className="font-normal">
                Ask before your coach ticks off a step
              </Label>
            </div>
            <p className="text-xs text-muted-foreground">
              Finishing the last step finishes the task, so this stays on unless you turn it
              off. Worth turning off for a project of short, obvious steps.
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="flex items-center gap-2">
        <Switch
          id="show-troubleshooting"
          checked={showTroubleshooting}
          onCheckedChange={(checked) => setShowTroubleshooting(Boolean(checked))}
        />
        <Label htmlFor="show-troubleshooting" className="font-normal">
          Show troubleshooting settings
        </Label>
      </div>

      {showTroubleshooting ? <TroubleshootingSection projectId={projectId} /> : null}
    </div>
  );
}
