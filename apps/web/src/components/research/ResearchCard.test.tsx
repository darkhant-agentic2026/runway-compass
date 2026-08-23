/**
 * The compact "latest research" card, shared by the board and the task workspace.
 *
 * docs/06-frontend.md#task-board-projectsprojectid: a brief description, a creation date,
 * and a status, linking into the run's own research view.
 */

import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ResearchCard } from '@/components/research/ResearchCard';
import { createQueryClient } from '@/features/queries';
import { api } from '@/lib/api';
import type { AutonomousRun } from '@/lib/schemas';
import { makeReport } from '@/test/factories';

function makeRun(overrides: Partial<AutonomousRun> = {}): AutonomousRun {
  return {
    id: 'r_1',
    ownerUid: 'u_alice',
    projectId: 'p_1',
    taskId: 'k_1',
    trigger: 'manual',
    mode: 'inline',
    status: 'complete',
    attempts: 1,
    maxAttempts: 3,
    steps: [],
    turnId: 't_1',
    sessionId: 's_research_1',
    changes: [],
    undoneAt: null,
    createdAt: '2026-08-20T10:00:00Z',
    updatedAt: null,
    error: null,
    ...overrides,
  };
}

function renderCard(run: AutonomousRun | undefined) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(<ResearchCard projectId="p_1" run={run} />, { wrapper });
}

afterEach(() => vi.restoreAllMocks());

describe('ResearchCard', () => {
  it('renders nothing when there is no run yet', () => {
    const { container } = renderCard(undefined);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a running run as in progress, with no report fetch', () => {
    const spy = vi.spyOn(api, 'getRunReport');
    renderCard(makeRun({ status: 'running' }));

    expect(screen.getByText('Your coach is researching…')).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it('shows a failed run as an offer to try again, not an error', () => {
    renderCard(makeRun({ status: 'failed' }));
    expect(screen.getByText("Couldn't finish — try again")).toBeInTheDocument();
  });

  it('shows the report summary for a completed run', async () => {
    vi.spyOn(api, 'getRunReport').mockResolvedValue(
      makeReport({ summary: 'Two things to get through.' }),
    );
    renderCard(makeRun({ status: 'complete' }));

    expect(await screen.findByText('Two things to get through.')).toBeInTheDocument();
  });

  it('links to the run’s own research view', () => {
    renderCard(makeRun({ id: 'r_42', status: 'failed' }));
    expect(screen.getByTestId('research-card')).toHaveAttribute(
      'href',
      '/projects/p_1/research/r_42',
    );
  });

  it('marks a project-scoped run (no taskId) distinctly', () => {
    renderCard(makeRun({ status: 'failed', taskId: null }));
    expect(screen.getByText('Project')).toBeInTheDocument();
  });
});
