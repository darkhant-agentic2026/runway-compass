/**
 * A coach message, rendered.
 *
 * docs/06-frontend.md#markdown-in-the-transcript owns the decisions; this file is where
 * they are spelled out as plugins and an element map. The two that are easy to undo by
 * accident:
 *
 * - **No `rehype-raw`, ever.** This renders text a language model produced, some of it
 *   quoted from pages the coach fetched. Without that plugin react-markdown leaves
 *   embedded HTML as text, which is exactly what we want; adding it — to make one table
 *   render, say — turns the transcript into an injection surface.
 * - **`urlTransform` is left at its default**, which drops `javascript:` and other
 *   non-http schemes from links and images. A custom one that "just allows everything"
 *   removes the same protection by a different door.
 *
 * Headings start at `<h3>` because the pane itself is an `<h2>` (`SessionPane`), so a
 * screen reader's outline stays truthful no matter what level the coach wrote.
 */

import { isValidElement, useMemo, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

import { CodeBlock } from '@/components/markdown/CodeBlock';
import { cn } from '@/lib/utils';

import 'katex/dist/katex.min.css';

/** Flatten a rendered node back to the text it was built from. */
function textOf(node: ReactNode): string {
  if (typeof node === 'string') return node;
  if (typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children);
  return '';
}

interface CodeProps {
  className?: string;
  children?: ReactNode;
}

/**
 * The `<code>` react-markdown built for a fenced block, read back off the `<pre>`.
 *
 * A fenced block is `<pre><code class="language-x">` in the tree, and `components.pre`
 * receives the *rendered* child — so its props are the ones react-markdown passed down,
 * language class included. Intercepting at the `pre` level rather than at `code` is what
 * distinguishes a block from inline code without guessing: react-markdown 10 no longer
 * passes an `inline` flag, and a fence with no language has no class to test either.
 */
function fenceOf(children: ReactNode): { code: string; lang: string } | null {
  const child = Array.isArray(children) ? children[0] : children;
  if (!isValidElement<CodeProps>(child)) return null;
  const lang = /language-([\w+#-]+)/.exec(child.props.className ?? '')?.[1] ?? '';
  // Markdown fences end in a newline that is part of the syntax, not of the code.
  return { code: textOf(child.props.children).replace(/\n$/, ''), lang };
}

function buildComponents(streaming: boolean): Components {
  return {
    pre({ children }) {
      const fence = fenceOf(children);
      if (!fence) return <pre>{children}</pre>;
      return <CodeBlock code={fence.code} lang={fence.lang} streaming={streaming} />;
    },
    code({ children, className }) {
      return (
        <code
          className={cn(
            'rounded bg-background/60 px-1 py-0.5 font-mono text-[0.9em]',
            className,
          )}
        >
          {children}
        </code>
      );
    },
    a({ children, href }) {
      return (
        <a
          href={href}
          target="_blank"
          rel="noreferrer noopener"
          className="underline underline-offset-2"
        >
          {children}
        </a>
      );
    },
    // A table in a chat bubble is the one element guaranteed not to fit: it scrolls
    // inside its own box rather than widening the pane and pushing the composer off
    // screen.
    table({ children }) {
      return (
        <div className="my-2 overflow-x-auto">
          <table className="w-full border-collapse text-left text-xs">{children}</table>
        </div>
      );
    },
    th({ children }) {
      return <th className="border px-2 py-1 font-semibold">{children}</th>;
    },
    td({ children }) {
      return <td className="border px-2 py-1 align-top">{children}</td>;
    },
    h1({ children }) {
      return <h3 className="mt-3 mb-1 text-sm font-semibold first:mt-0">{children}</h3>;
    },
    h2({ children }) {
      return <h4 className="mt-3 mb-1 text-sm font-semibold first:mt-0">{children}</h4>;
    },
    h3({ children }) {
      return <h5 className="mt-3 mb-1 text-sm font-semibold first:mt-0">{children}</h5>;
    },
    h4({ children }) {
      return <h6 className="mt-3 mb-1 text-sm font-semibold first:mt-0">{children}</h6>;
    },
    p({ children }) {
      return <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>;
    },
    ul({ children }) {
      return <ul className="my-1.5 list-disc space-y-0.5 pl-5">{children}</ul>;
    },
    ol({ children }) {
      return <ol className="my-1.5 list-decimal space-y-0.5 pl-5">{children}</ol>;
    },
    blockquote({ children }) {
      return <blockquote className="my-2 border-l-2 pl-3 italic">{children}</blockquote>;
    },
    hr() {
      return <hr className="my-3" />;
    },
    input({ checked, type }) {
      // GFM task lists. Rendered read-only: a checkbox in the transcript would look like
      // it tracked something, and the task board is where completion actually lives.
      return <input type={type} checked={checked} readOnly className="mr-1 align-middle" />;
    },
  };
}

export function Markdown({
  text,
  streaming = false,
  className,
}: {
  text: string;
  /** True while the turn that produced this text is still generating. */
  streaming?: boolean;
  className?: string;
}) {
  // Rebuilt only when the streaming flag flips, so a delta does not hand react-markdown a
  // new component map on every frame.
  const components = useMemo(() => buildComponents(streaming), [streaming]);

  return (
    <div className={cn('break-words', className)} data-testid="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
