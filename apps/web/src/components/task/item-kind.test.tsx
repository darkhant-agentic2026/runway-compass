import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ItemKindBadge, ItemKindStrip } from '@/components/task/item-kind';

describe('ItemKindBadge', () => {
  it('shows the icon and label for one kind', () => {
    render(<ItemKindBadge kind="video" />);
    expect(screen.getByTestId('item-kind')).toHaveTextContent('Video');
  });
});

describe('ItemKindStrip', () => {
  it('groups required items by kind, counting duplicates, and always shows the count', () => {
    render(
      <ItemKindStrip
        required={[{ kind: 'article' }, { kind: 'article' }, { kind: 'video' }]}
      />,
    );

    const required = screen.getByTestId('item-kind-required');
    // Two count markers: "2x" for the articles, "1x" for the video — the count shows even
    // for a single item, unlike the old "hide 1x" behaviour.
    expect(required.textContent?.match(/x/g)).toHaveLength(2);
    expect(required).toHaveTextContent('2x');
    expect(required).toHaveTextContent('1x');
    expect(screen.queryByTestId('item-kind-optional')).not.toBeInTheDocument();
  });

  it('shows optional items as a second, separate group, introduced by a "+"', () => {
    render(
      <ItemKindStrip
        required={[{ kind: 'article' }]}
        optional={[{ kind: 'video' }, { kind: 'exercise' }]}
      />,
    );

    const chip = screen.getByTestId('item-kind-summary');
    expect(within(chip).getByTestId('item-kind-required')).toHaveTextContent('1x');
    const optional = within(chip).getByTestId('item-kind-optional');
    expect(optional.textContent?.match(/x/g)).toHaveLength(2);
    expect(chip).toHaveTextContent('+');
  });

  it('renders only the required group when there is no optional material', () => {
    render(<ItemKindStrip required={[{ kind: 'article' }]} />);
    expect(screen.queryByText('+')).not.toBeInTheDocument();
    expect(screen.queryByTestId('item-kind-optional')).not.toBeInTheDocument();
  });

  it('shows duration only when given, and nothing at all with no minutes and no items', () => {
    const { rerender } = render(<ItemKindStrip minutes={45} required={[]} />);
    expect(screen.getByTestId('item-kind-strip')).toHaveTextContent('45 min');
    expect(screen.queryByTestId('item-kind-summary')).not.toBeInTheDocument();

    rerender(<ItemKindStrip required={[{ kind: 'doc' }]} />);
    expect(screen.getByTestId('item-kind-summary')).toBeInTheDocument();

    rerender(<ItemKindStrip required={[]} />);
    expect(screen.queryByTestId('item-kind-strip')).not.toBeInTheDocument();
  });

  it('ignores items with no kind (hand-added checklist entries)', () => {
    render(<ItemKindStrip required={[{ kind: null }, { kind: undefined }]} />);
    expect(screen.queryByTestId('item-kind-summary')).not.toBeInTheDocument();
  });
});
