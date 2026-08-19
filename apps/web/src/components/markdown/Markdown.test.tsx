/**
 * Rendered markdown.
 *
 * docs/08-testing.md:
 *
 * > **Markdown rendering** — a GFM table becomes a `<table>`, `$…$` becomes KaTeX output,
 * > a fenced block keeps its code whether or not the highlighter ever loads, and raw HTML
 * > in a message stays inert text. Plus the streaming rule: a ` ```mermaid ` fence is a
 * > code block while its turn is generating and a diagram once it has settled. The dynamic
 * > imports are mocked, so the tests assert *this* module's decisions rather than shiki's
 * > and mermaid's output.
 *
 * The mocks are the point of this file's shape. Both libraries are reached through
 * `import()` at render time, so a test that let the real ones load would be measuring
 * whether shiki's grammar for Python is any good — while the decisions that can actually
 * regress here are ours: which fence becomes a diagram and when, what is left when a load
 * fails, and what never becomes markup.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Markdown } from '@/components/markdown/Markdown';
import { resetHighlighterForTests } from '@/lib/highlighter';

const mermaidRender = vi.fn();

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: (id: string, code: string) => mermaidRender(id, code),
  },
}));

/** A highlighter with one language, returning one token per line. */
vi.mock('shiki', () => ({
  bundledLanguages: { python: () => Promise.resolve({}) },
  createHighlighter: () =>
    Promise.resolve({
      getLoadedLanguages: () => ['python'],
      loadLanguage: () => Promise.resolve(),
      codeToTokens: (code: string) => ({
        tokens: code.split('\n').map((line) => [
          {
            content: line,
            htmlStyle: { '--shiki-light': '#005cc5', '--shiki-dark': '#79b8ff' },
          },
        ]),
      }),
    }),
}));

vi.mock('shiki/engine/javascript', () => ({
  createJavaScriptRegexEngine: () => ({}),
}));

beforeEach(() => {
  resetHighlighterForTests();
  mermaidRender.mockReset();
  mermaidRender.mockResolvedValue({ svg: '<svg data-testid="rendered-svg"></svg>' });
});

afterEach(() => vi.clearAllMocks());

describe('block elements', () => {
  it('renders a GFM table as a table', () => {
    render(
      <Markdown
        text={['| Step | Minutes |', '| --- | --- |', '| Read the paper | 20 |'].join('\n')}
      />,
    );

    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Step' })).toBeInTheDocument();
    expect(screen.getByRole('cell', { name: 'Read the paper' })).toBeInTheDocument();
  });

  it('renders inline math through KaTeX', () => {
    const { container } = render(<Markdown text={'The bound is $O(n \\log n)$ here.'} />);

    // KaTeX's own container class rather than a rendered glyph: the glyphs are its
    // business, the fact that the equation was handed to it is ours.
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.textContent).not.toContain('$');
  });

  it('lists, headings, and links survive, and links leave the app safely', () => {
    render(<Markdown text={'## Plan\n\n- [docs](https://example.com/a)\n- second'} />);

    expect(screen.getByRole('heading', { name: 'Plan' })).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
    const link = screen.getByRole('link', { name: 'docs' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
  });
});

describe('what must never become markup', () => {
  it('leaves embedded HTML as text', () => {
    const { container } = render(
      <Markdown text={'Careful: <img src="x" onerror="alert(1)"> and <b>bold</b>.'} />,
    );

    // No `rehype-raw`, so none of this is an element. The transcript renders model
    // output, and this is the single assertion standing between that and injection.
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(container.textContent).toContain('<img src="x" onerror="alert(1)">');
  });

  it('drops a javascript: link target', () => {
    render(<Markdown text={'[click](javascript:alert(1))'} />);

    // react-markdown's default `urlTransform`. Asserted here because the way it gets
    // lost is someone passing a permissive one to fix an unrelated link.
    //
    // By text rather than by role: the transform empties the `href`, and an anchor with
    // an empty `href` no longer computes as a link — which is itself the outcome we want.
    expect(screen.getByText('click').closest('a')).toHaveAttribute('href', '');
  });
});

describe('code', () => {
  it('keeps inline code inline', () => {
    const { container } = render(<Markdown text={'Call `asyncio.run()` to start.'} />);

    expect(screen.queryByTestId('code-block')).not.toBeInTheDocument();
    expect(container.querySelector('code')?.textContent).toBe('asyncio.run()');
  });

  it('shows the code before the highlighter resolves, and highlights it after', async () => {
    render(<Markdown text={'```python\nprint("hi")\n```'} />);

    // The unhighlighted block is the initial state, not an error branch.
    const block = screen.getByTestId('code-block');
    expect(block).toHaveTextContent('print("hi")');

    await waitFor(() => {
      expect(block.querySelector('code')).toHaveClass('shiki');
    });
    expect(block.querySelector('span[style]')?.getAttribute('style')).toContain(
      '--shiki-light',
    );
  });

  it('still shows the code when the language is unknown to the highlighter', async () => {
    render(<Markdown text={'```brainfuck\n+[-]\n```'} />);

    const block = screen.getByTestId('code-block');
    expect(block).toHaveTextContent('+[-]');
    // Never highlighted, so never marked as such — and never blank, which is the failure
    // that would matter.
    await waitFor(() => expect(block.querySelector('code')).not.toHaveClass('shiki'));
  });

  it('shows the code when the highlighter fails to load at all', async () => {
    resetHighlighterForTests();
    const shiki = await import('shiki');
    vi.spyOn(shiki, 'createHighlighter').mockRejectedValue(new Error('offline'));

    render(<Markdown text={'```python\nprint("hi")\n```'} />);

    const block = screen.getByTestId('code-block');
    expect(block).toHaveTextContent('print("hi")');
    await waitFor(() => expect(block.querySelector('code')).not.toHaveClass('shiki'));
  });
});

describe('mermaid, and when a fence becomes a diagram', () => {
  const DIAGRAM = '```mermaid\ngraph TD;\n  A-->B;\n```';

  it('stays a code block while the turn is still generating', () => {
    render(<Markdown text={DIAGRAM} streaming />);

    expect(screen.getByTestId('code-block')).toHaveTextContent('graph TD;');
    expect(screen.queryByTestId('mermaid-diagram')).not.toBeInTheDocument();
    // The point of the rule: a half-written definition is never handed to the parser.
    expect(mermaidRender).not.toHaveBeenCalled();
  });

  it('renders as a diagram once the turn has settled', async () => {
    render(<Markdown text={DIAGRAM} />);

    await waitFor(() => expect(screen.getByTestId('mermaid-diagram')).toBeInTheDocument());
    expect(mermaidRender).toHaveBeenCalledWith(expect.any(String), 'graph TD;\n  A-->B;');
  });

  it('falls back to the definition when mermaid cannot parse it', async () => {
    mermaidRender.mockRejectedValue(new Error('Parse error'));

    render(<Markdown text={'```mermaid\nnot a diagram\n```'} />);

    await waitFor(() => expect(screen.getByTestId('mermaid-source')).toBeInTheDocument());
    expect(screen.getByTestId('mermaid-source')).toHaveTextContent('not a diagram');
    expect(screen.queryByTestId('mermaid-diagram')).not.toBeInTheDocument();
  });
});

describe('soft breaks, for the learner’s own messages', () => {
  /**
   * Markdown treats a single newline as a space. Someone typing into a chat box presses
   * Enter to break a line — and "it would collapse the line breaks they put in" was the
   * recorded reason for not rendering their messages at all, so answering it is what makes
   * the reversal safe rather than merely desired.
   */
  it('turns a single newline into a line break when asked', () => {
    const { container } = render(<Markdown text={'first line\nsecond line'} softBreaks />);
    expect(container.querySelectorAll('br')).toHaveLength(1);
  });

  it('leaves the coach’s text alone', () => {
    // The coach writes markdown deliberately, so its output means what markdown says.
    const { container } = render(<Markdown text={'first line\nsecond line'} />);
    expect(container.querySelectorAll('br')).toHaveLength(0);
  });

  it('still renders a fenced block verbatim with soft breaks on', () => {
    // The case the original objection was really about: a pasted traceback must not be
    // reflowed, and `remark-breaks` must not reach inside code.
    const { container } = render(
      <Markdown text={'```\nTraceback\n  File "x.py"\n```'} softBreaks />,
    );
    expect(container.querySelector('code')?.textContent).toContain('File "x.py"');
    expect(container.querySelectorAll('code br')).toHaveLength(0);
  });
});
