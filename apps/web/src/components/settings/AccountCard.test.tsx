import { QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AccountCard } from '@/components/settings/AccountCard';
import { createQueryClient } from '@/features/queries';
import { api } from '@/lib/api';
import type { Me } from '@/lib/schemas';
import { makeUsageStatus } from '@/test/factories';

function makeMe(overrides: Partial<Me> = {}): Me {
  return {
    uid: 'u_alice',
    email: 'alice@example.com',
    displayName: 'Alice',
    photoUrl: null,
    globalPrefs: {
      defaultTaskMinutes: 45,
      guidanceStyle: 'socratic',
      verbosity: 'balanced',
      timezone: 'UTC',
      autonomousEnabled: true,
      autonomousQuietHours: { start: '23:00', end: '07:00' },
    },
    learnerProfile: {
      thinkingStyle: '',
      strengths: [],
      gaps: [],
      technologies: [],
      pacing: '',
      feedbackNotes: [],
      updatedAt: null,
      updatedBy: 'user',
      version: 0,
    },
    plan: {
      tier: 'free',
      limits: {
        autonomousRunsPerDay: 20,
        monthlyPoints: 500,
        dailyPoints: 200,
        fourHourPoints: 80,
      },
    },
    usage: makeUsageStatus(),
    ...overrides,
  };
}

function renderCard(me = makeMe()) {
  const queryClient = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(<AccountCard me={me} />, { wrapper });
}

describe('AccountCard', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the current display name and the read-only email', () => {
    renderCard(makeMe({ displayName: 'Alice Example', email: 'alice@example.com' }));

    expect(screen.getByLabelText('Display name')).toHaveValue('Alice Example');
    const emailField = screen.getByLabelText('Email');
    expect(emailField).toHaveValue('alice@example.com');
    expect(emailField).toBeDisabled();
  });

  it('saves a trimmed display name on blur', async () => {
    const patchSpy = vi
      .spyOn(api, 'patchDisplayName')
      .mockResolvedValue(makeMe({ displayName: 'Jane Doe' }));

    renderCard(makeMe({ displayName: 'Alice' }));
    const field = screen.getByLabelText('Display name');
    fireEvent.change(field, { target: { value: '  Jane Doe  ' } });
    fireEvent.blur(field);

    await waitFor(() => expect(patchSpy).toHaveBeenCalledWith('Jane Doe', expect.any(String)));
  });

  it('does not save when the value is unchanged or blank', () => {
    const patchSpy = vi.spyOn(api, 'patchDisplayName').mockResolvedValue(makeMe());

    renderCard(makeMe({ displayName: 'Alice' }));
    const field = screen.getByLabelText('Display name');

    fireEvent.blur(field); // unchanged
    fireEvent.change(field, { target: { value: '   ' } });
    fireEvent.blur(field); // blank after trimming

    expect(patchSpy).not.toHaveBeenCalled();
  });
});
