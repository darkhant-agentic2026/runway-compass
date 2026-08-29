/**
 * `Project['status']`, client side.
 *
 * The chip and its tooltip exist so the autonomous scheduler's presence/status guard
 * (`project.status == "active"`, docs/05-autonomous-runs.md#candidate-selection-and-guards)
 * is *visible* on a project that isn't running autonomously, not just true —
 * docs/09-roadmap.md#project-state-and-archiving.
 */

import { Archive, CircleDot, PauseCircle, type LucideIcon } from 'lucide-react';

import type { Project } from '@/lib/schemas';

export type ProjectStatus = Project['status'];

export const PROJECT_STATE_LABELS: Record<ProjectStatus, string> = {
  active: 'Active',
  paused: 'Paused',
  archived: 'Archived',
};

export interface ProjectStateAccent {
  icon: LucideIcon;
  /** Applied to both the icon and the label, so the colour carries the whole chip. */
  className: string;
}

export const PROJECT_STATE_ACCENT: Record<ProjectStatus, ProjectStateAccent> = {
  active: { icon: CircleDot, className: 'text-muted-foreground' },
  paused: { icon: PauseCircle, className: 'text-status-paused' },
  archived: { icon: Archive, className: 'text-muted-foreground' },
};

/** Tooltip copy for `ProjectStateChip` — the guard is real for every status, but only
 * `paused`/`archived` change what it actually skips. */
export const PROJECT_STATE_GUARD_HINT: Record<ProjectStatus, string> = {
  active: 'Eligible for autonomous research and task proposals.',
  paused:
    "Paused projects are skipped by the autonomous scheduler's presence/status guard — no automatic research or task proposals until you reactivate it.",
  archived:
    "Archived projects are skipped by the autonomous scheduler's presence/status guard, the same as paused. Restore it from Archived projects to resume.",
};
