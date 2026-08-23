/**
 * The compact "latest research" card, shared by the board and the task workspace, and
 * the "View previous research" disclosure beside it.
 *
 * docs/06-frontend.md#task-board-projectsprojectid: a brief description, a creation date,
 * and a status, linking into the run's own research view. Earlier runs are the same idea,
 * behind a toggle rather than always on screen.
 */

import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ResearchCard } from '@/components/research/ResearchCard';
import { createQueryClient } from '@/features/queries';
import { api } from '@/lib/api';
import type { AutonomousRun } from '@/lib/schemas';
import { makeReport } from '@/test/factories';

let counter = 0;

function makeRun(overrides: Partial<AutonomousRun> = {}): AutonomousRun {
  counter += 1;
  return {
    id: `r_${counter}`,
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

function renderCard(runs: AutonomousRun[]) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(<ResearchCard projectId="p_1" runs={runs} />, { wrapper });
}

afterEach(() => vi.restoreAllMocks());

describe('ResearchCard', () => {
  it('renders nothing when there is no run yet', () => {
    const { container } = renderCard([]);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a running run as in progress, with no report fetch', () => {
    const spy = vi.spyOn(api, 'getRunReport');
    renderCard([makeRun({ status: 'running' })]);

    expect(screen.getByText('Your coach is researching…')).toBeInTheDocument();
    expect(spy).not.toHaveBeenCalled();
  });

  it('shows a failed run as an offer to try again, not an error', () => {
    renderCard([makeRun({ status: 'failed' })]);
    expect(screen.getByText("Couldn't finish — try again")).toBeInTheDocument();
  });

  it('shows the report summary for a completed run', async () => {
    vi.spyOn(api, 'getRunReport').mockResolvedValue(
      makeReport({ summary: 'Two things to get through.' }),
    );
    renderCard([makeRun({ status: 'complete' })]);

    expect(await screen.findByText('Two things to get through.')).toBeInTheDocument();
  });

  it('links to the run’s own research view', () => {
    renderCard([makeRun({ id: 'r_42', status: 'failed' })]);
    expect(screen.getByTestId('research-card')).toHaveAttribute(
      'href',
      '/projects/p_1/research/r_42',
    );
  });

  it('marks a project-scoped run (no taskId) distinctly', () => {
    renderCard([makeRun({ status: 'failed', taskId: null })]);
    expect(screen.getByText('Project')).toBeInTheDocument();
  });

  describe('previous research', () => {
    it('offers no toggle when there is only one run', () => {
      renderCard([makeRun()]);
      expect(screen.queryByTestId('toggle-previous-research')).not.toBeInTheDocument();
    });

    it('names how many earlier runs there are, and lists none of them until asked', () => {
      const spy = vi.spyOn(api, 'getRunReport');
      const latest = makeRun();
      renderCard([latest, makeRun({ status: 'failed' }), makeRun({ status: 'complete' })]);

      expect(screen.getByText('View previous research (2)')).toBeInTheDocument();
      expect(screen.queryByTestId('previous-research')).not.toBeInTheDocument();
      // The visible card fetches its own report; collapsed, neither earlier run does.
      expect(spy).toHaveBeenCalledTimes(1);
      expect(spy).toHaveBeenCalledWith(latest.id);
    });

    it('expands to a card per earlier run, each linking to its own research view', async () => {
      const latest = makeRun({ id: 'r_latest', status: 'running' });
      const middle = makeRun({ id: 'r_middle', status: 'failed' });
      const oldest = makeRun({ id: 'r_oldest', status: 'complete' });
      vi.spyOn(api, 'getRunReport').mockResolvedValue(
        makeReport({ summary: 'The first attempt.' }),
      );

      renderCard([latest, middle, oldest]);
      await userEvent.click(screen.getByTestId('toggle-previous-research'));

      const list = screen.getByTestId('previous-research');
      expect(list).toBeInTheDocument();

      const cards = screen.getAllByTestId('research-card');
      // The latest card, plus one per earlier run.
      expect(cards).toHaveLength(3);
      expect(cards[1]).toHaveAttribute('href', '/projects/p_1/research/r_middle');
      expect(cards[2]).toHaveAttribute('href', '/projects/p_1/research/r_oldest');
      expect(await screen.findByText('The first attempt.')).toBeInTheDocument();

      await userEvent.click(screen.getByTestId('toggle-previous-research'));
      expect(screen.queryByTestId('previous-research')).not.toBeInTheDocument();
    });
  });
});
