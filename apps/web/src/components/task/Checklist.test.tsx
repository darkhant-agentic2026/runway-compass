/**
 * The checklist, and the one rendering rule that is a safety property rather than a style.
 *
 * docs/08-testing.md: "**A guided item does not render its `details`** — the coach's
 * teaching notes are not the learner's instructions, and the exercise's answer is in
 * there." Asserted as an absence, with the same string present on an unguided item in the
 * same render, so the test fails if the component stops rendering `details` at all rather
 * than passing vacuously.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Checklist } from '@/components/task/Checklist';
import { makeTaskItem } from '@/test/factories';

const SECRET = 'Ask them to explain it back before showing them the answer.';

describe('Checklist', () => {
  it('renders an unguided item’s details and link, because those are the instruction', () => {
    render(
      <Checklist
        items={[
          makeTaskItem({
            shortDescription: 'Read the official guide',
            details: SECRET,
            url: 'https://example.com/guide',
            guided: false,
          }),
        ]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );

    expect(screen.getByText('Read the official guide')).toBeInTheDocument();
    expect(screen.getByTestId('item-details')).toHaveTextContent(SECRET);
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute(
      'href',
      'https://example.com/guide',
    );
  });

  it('never renders a guided item’s details', () => {
    render(
      <Checklist
        items={[
          makeTaskItem({
            itemId: 'i_guided',
            shortDescription: 'Work through the exercise',
            details: SECRET,
            guided: true,
          }),
          makeTaskItem({
            itemId: 'i_open',
            shortDescription: 'Read the guide',
            details: 'Sections 3 and 4 of the linked page.',
            guided: false,
          }),
        ]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );

    // The guided item is on screen…
    expect(screen.getByText('Work through the exercise')).toBeInTheDocument();
    expect(screen.getByText('with your coach')).toBeInTheDocument();
    // …its notes are not…
    expect(screen.queryByText(SECRET)).not.toBeInTheDocument();
    // …and the component has not simply stopped rendering details altogether, which is how
    // this assertion would otherwise pass while telling us nothing.
    expect(screen.getByTestId('item-details')).toHaveTextContent(
      'Sections 3 and 4 of the linked page.',
    );
  });

  it('a guided item still links its source, because the link is not the coach’s notes', () => {
    render(
      <Checklist
        items={[makeTaskItem({ guided: true, url: 'https://example.com/source' })]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute(
      'href',
      'https://example.com/source',
    );
  });

  it('the budget meter counts the checklist and nothing else', () => {
    render(
      <Checklist
        items={[
          makeTaskItem({ itemId: 'i_a', minutes: 15, completed: true }),
          makeTaskItem({ itemId: 'i_b', minutes: 30, completed: false }),
        ]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.getByTestId('checklist-budget')).toHaveTextContent(
      '1 of 2 done · 45 min of 45 min',
    );
  });

  it('ticking a box reports the item and the new value', async () => {
    const onToggle = vi.fn();
    render(
      <Checklist
        items={[makeTaskItem({ itemId: 'i_tick', shortDescription: 'Do the thing' })]}
        budgetMinutes={null}
        onToggle={onToggle}
      />,
    );

    await userEvent.click(screen.getByRole('checkbox', { name: 'Do the thing' }));
    expect(onToggle).toHaveBeenCalledWith('i_tick', true);
  });

  it('renders nothing at all when there is no checklist', () => {
    const { container } = render(
      <Checklist items={[]} budgetMinutes={45} onToggle={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows the kind chip when the item carries one, and nothing for a hand-added item', () => {
    render(
      <Checklist
        items={[
          makeTaskItem({
            itemId: 'i_kind',
            shortDescription: 'Watch the intro',
            kind: 'video',
          }),
          makeTaskItem({ itemId: 'i_no_kind', shortDescription: 'My own note', kind: null }),
        ]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );

    expect(screen.getByTestId('item-kind')).toHaveTextContent('Video');
    expect(screen.getAllByTestId('item-kind')).toHaveLength(1);
  });

  it('marks the first uncompleted item as "you are here" while the task is in progress', () => {
    render(
      <Checklist
        items={[
          makeTaskItem({ itemId: 'i_done', shortDescription: 'Read §3', completed: true }),
          makeTaskItem({ itemId: 'i_next', shortDescription: 'Do the exercise' }),
          makeTaskItem({ itemId: 'i_later', shortDescription: 'Write it up' }),
        ]}
        budgetMinutes={45}
        onToggle={vi.fn()}
        inProgress
      />,
    );

    const marked = screen.getByTestId('you-are-here');
    expect(marked).toHaveTextContent('Do the exercise');
  });

  it('marks nothing when the task is not in progress', () => {
    render(
      <Checklist
        items={[makeTaskItem({ itemId: 'i_next', shortDescription: 'Do the exercise' })]}
        budgetMinutes={45}
        onToggle={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('you-are-here')).not.toBeInTheDocument();
  });

  describe('the "you are here" rule slides rather than jumps', () => {
    /*
     * jsdom does no real layout, so `getBoundingClientRect` is stubbed: the checklist's
     * container (`data-testid="checklist-items"`) and whichever item is currently marked
     * `you-are-here` are the only two elements the component measures.
     */
    function stubRects(container: { top: number }, active: { top: number; height: number }) {
      const rect = (r: { top: number; height?: number }) =>
        ({
          top: r.top,
          height: r.height ?? 0,
          bottom: 0,
          left: 0,
          right: 0,
          width: 0,
          x: 0,
          y: r.top,
          toJSON: () => ({}),
        }) as DOMRect;

      vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
        this: HTMLElement,
      ) {
        if (this.dataset.testid === 'checklist-items') return rect(container);
        if (this.dataset.testid === 'you-are-here') return rect(active);
        return rect({ top: 0, height: 0 });
      });
    }

    afterEach(() => {
      vi.restoreAllMocks();
    });

    it('positions the line at the current item, relative to the container', () => {
      stubRects({ top: 10 }, { top: 40, height: 20 });

      render(
        <Checklist
          items={[
            makeTaskItem({ itemId: 'i_done', shortDescription: 'Read §3', completed: true }),
            makeTaskItem({ itemId: 'i_next', shortDescription: 'Do the exercise' }),
          ]}
          budgetMinutes={45}
          onToggle={vi.fn()}
          inProgress
        />,
      );

      const line = screen.getByTestId('you-are-here-line');
      expect(line.style.top).toBe('30px');
      expect(line.style.height).toBe('20px');
    });

    it('moves the line when a different item becomes current', () => {
      stubRects({ top: 0 }, { top: 40, height: 20 });

      const { rerender } = render(
        <Checklist
          items={[
            makeTaskItem({ itemId: 'i_a', shortDescription: 'First', completed: false }),
            makeTaskItem({ itemId: 'i_b', shortDescription: 'Second', completed: false }),
          ]}
          budgetMinutes={45}
          onToggle={vi.fn()}
          inProgress
        />,
      );
      expect(screen.getByTestId('you-are-here-line').style.top).toBe('40px');
      expect(screen.getByTestId('you-are-here')).toHaveTextContent('First');

      // "First" completes — "Second" is now the current item, at a different position.
      stubRects({ top: 0 }, { top: 90, height: 20 });
      rerender(
        <Checklist
          items={[
            makeTaskItem({ itemId: 'i_a', shortDescription: 'First', completed: true }),
            makeTaskItem({ itemId: 'i_b', shortDescription: 'Second', completed: false }),
          ]}
          budgetMinutes={45}
          onToggle={vi.fn()}
          inProgress
        />,
      );

      expect(screen.getByTestId('you-are-here')).toHaveTextContent('Second');
      expect(screen.getByTestId('you-are-here-line').style.top).toBe('90px');
    });
  });
});
