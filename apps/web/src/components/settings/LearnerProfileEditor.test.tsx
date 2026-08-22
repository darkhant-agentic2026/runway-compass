import { QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { LearnerProfileEditor } from '@/components/settings/LearnerProfileEditor';
import { createQueryClient } from '@/features/queries';
import { api } from '@/lib/api';
import { makeLearnerProfile } from '@/test/factories';

function renderEditor(profile = makeLearnerProfile()) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<LearnerProfileEditor profile={profile} />, { wrapper });
}

describe('LearnerProfileEditor', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('renders existing learner profile fields', () => {
    const profile = makeLearnerProfile({
      thinkingStyle: 'Visual and hands-on',
      strengths: ['Python', 'SQL'],
      gaps: ['Distributed systems'],
      technologies: [
        { name: 'Postgres', level: 'advanced', evidence: 'Optimized query plans' },
      ],
      pacing: 'Fast-paced',
      feedbackNotes: ['Learner grasps indexing quickly'],
      version: 3,
      updatedBy: 'agent',
    });

    renderEditor(profile);

    expect(screen.getByText('What your coach knows about you')).toBeInTheDocument();
    expect(screen.getByText('Version 3')).toBeInTheDocument();
    expect(screen.getByText(/Updated by Coach/)).toBeInTheDocument();
    expect(screen.getByDisplayValue('Visual and hands-on')).toBeInTheDocument();
    expect(screen.getByText('Python')).toBeInTheDocument();
    expect(screen.getByText('SQL')).toBeInTheDocument();
    expect(screen.getByText('Distributed systems')).toBeInTheDocument();
    expect(screen.getByText('Postgres')).toBeInTheDocument();
    expect(screen.getByText('Optimized query plans')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Fast-paced')).toBeInTheDocument();
    expect(screen.getByText('Learner grasps indexing quickly')).toBeInTheDocument();
  });

  it('allows adding and removing a strength', async () => {
    const patchSpy = vi
      .spyOn(api, 'patchLearnerProfile')
      .mockResolvedValue(makeLearnerProfile());
    const profile = makeLearnerProfile({ strengths: ['Rust'] });

    renderEditor(profile);

    // Add strength
    const input = screen.getByPlaceholderText(/Add a strength/i);
    fireEvent.change(input, { target: { value: 'Go concurrency' } });
    fireEvent.click(screen.getByRole('button', { name: /Add strength/i }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(
        expect.objectContaining({ strengths: ['Rust', 'Go concurrency'] }),
      );
    });

    // Remove strength
    const removeBtn = screen.getByRole('button', { name: /Remove strength Rust/i });
    fireEvent.click(removeBtn);

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(expect.objectContaining({ strengths: [] }));
    });
  });

  it('allows adding and removing a technology belief', async () => {
    const patchSpy = vi
      .spyOn(api, 'patchLearnerProfile')
      .mockResolvedValue(makeLearnerProfile());
    const profile = makeLearnerProfile({
      technologies: [{ name: 'Docker', level: 'intermediate', evidence: 'Multi-stage builds' }],
    });

    renderEditor(profile);

    // Add tech
    fireEvent.change(screen.getByPlaceholderText(/Technology name/i), {
      target: { value: 'Kubernetes' },
    });
    fireEvent.change(screen.getByPlaceholderText(/Evidence\/context/i), {
      target: { value: 'Helm deployments' },
    });
    fireEvent.click(screen.getByRole('button', { name: /Add technology/i }));

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          technologies: [
            { name: 'Docker', level: 'intermediate', evidence: 'Multi-stage builds' },
            { name: 'Kubernetes', level: 'intermediate', evidence: 'Helm deployments' },
          ],
        }),
      );
    });

    // Remove tech
    fireEvent.click(screen.getByRole('button', { name: /Remove technology Docker/i }));
    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith(expect.objectContaining({ technologies: [] }));
    });
  });

  it('start fresh resets all learner profile fields', async () => {
    const patchSpy = vi
      .spyOn(api, 'patchLearnerProfile')
      .mockResolvedValue(makeLearnerProfile());
    const profile = makeLearnerProfile({
      thinkingStyle: 'Some style',
      strengths: ['Topic A'],
      gaps: ['Topic B'],
      pacing: 'Fast',
    });

    renderEditor(profile);

    const startFreshBtn = screen.getByRole('button', { name: /Start fresh/i });
    fireEvent.click(startFreshBtn);

    await waitFor(() => {
      expect(patchSpy).toHaveBeenCalledWith({
        thinkingStyle: '',
        strengths: [],
        gaps: [],
        technologies: [],
        pacing: '',
        feedbackNotes: [],
      });
    });
  });
});
