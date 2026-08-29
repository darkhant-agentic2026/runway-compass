/**
 * The state label shared by `TaskCard` and `TaskInfoStrip` (`pages/TaskWorkspacePage.tsx`),
 * so both screens colour `completed`/`in_progress` the same way.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { StateLabel } from '@/components/board/StateLabel';

describe('StateLabel', () => {
  it('colours "Completed" and gives it a checkmark icon', () => {
    render(<StateLabel state="completed" />);
    const badge = screen.getByTestId('state-badge');
    expect(badge).toHaveTextContent('Completed');
    expect(badge).toHaveClass('text-status-completed');
    expect(badge.querySelector('svg')).toBeInTheDocument();
  });

  it('colours "In progress" and gives it a dot icon', () => {
    render(<StateLabel state="in_progress" />);
    const badge = screen.getByTestId('state-badge');
    expect(badge).toHaveTextContent('In progress');
    expect(badge).toHaveClass('text-progress-fill');
    expect(badge.querySelector('svg')).toBeInTheDocument();
  });

  it('leaves every other state as plain text, with no icon', () => {
    render(<StateLabel state="postponed" />);
    const badge = screen.getByTestId('state-badge');
    expect(badge).toHaveTextContent('Postponed');
    expect(badge.querySelector('svg')).not.toBeInTheDocument();
  });
});
