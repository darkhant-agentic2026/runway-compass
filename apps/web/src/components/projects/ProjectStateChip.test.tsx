/**
 * A project's state chip — docs/09-roadmap.md#project-state-and-archiving. The chip
 * exists so a `paused`/`archived` project's exemption from the autonomous scheduler's
 * presence/status guard is visible, not just true; this pins which label and `data-status`
 * each state renders, which is what `ProjectsPage`/`ArchivedProjectsPage`/`BoardPage` and
 * their e2e coverage key off of.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ProjectStateChip } from '@/components/projects/ProjectStateChip';
import type { Project } from '@/lib/schemas';

describe('ProjectStateChip', () => {
  it.each<[Project['status'], string]>([
    ['active', 'Active'],
    ['paused', 'Paused'],
    ['archived', 'Archived'],
  ])('labels %s as %s', (status, label) => {
    render(<ProjectStateChip status={status} />);
    const chip = screen.getByTestId('project-state-chip');
    expect(chip).toHaveTextContent(label);
    expect(chip).toHaveAttribute('data-status', status);
  });
});
