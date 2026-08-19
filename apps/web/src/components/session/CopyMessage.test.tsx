/**
 * The copy control.
 *
 * It carries more weight than a convenience: it is half the reason rendering the learner's
 * messages as markdown is safe. The transcript stopped showing their text verbatim, so the
 * thing they actually sent has to be recoverable — and that means the *source*, never the
 * rendered output.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CopyMessage } from '@/components/session/CopyMessage';

const writeText = vi.fn<(text: string) => Promise<void>>();

beforeEach(() => {
  writeText.mockReset().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
});

describe('CopyMessage', () => {
  it('copies the source text, not what was rendered from it', async () => {
    const source = '# Heading\n\n| a | b |\n| --- | --- |\n| 1 | 2 |';
    render(<CopyMessage text={source} />);

    await userEvent.click(screen.getByRole('button', { name: 'Copy message' }));
    expect(writeText).toHaveBeenCalledWith(source);
  });

  it('announces the copy, rather than only showing a tick', async () => {
    // The icon swap says nothing to a screen reader; the label is the confirmation.
    render(<CopyMessage text="hello" />);
    await userEvent.click(screen.getByRole('button', { name: 'Copy message' }));
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
  });

  it('says so when the browser refuses', async () => {
    // `navigator.clipboard` is absent on an insecure origin and can be denied by
    // permissions policy, so this path is real rather than defensive — and a copy button
    // that silently does nothing is worse than one that is not there.
    writeText.mockRejectedValue(new Error('denied'));
    const { toast } = await import('sonner');
    const error = vi.spyOn(toast, 'error').mockImplementation(() => '');

    render(<CopyMessage text="hello" />);
    await userEvent.click(screen.getByRole('button', { name: 'Copy message' }));

    expect(error).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Copy message' })).toBeInTheDocument();
  });
});
