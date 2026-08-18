/**
 * A mermaid diagram.
 *
 * The one place in the app that assigns `dangerouslySetInnerHTML`, and it is worth being
 * explicit about why that is acceptable here and nowhere else in the transcript: mermaid
 * does not pass the message text through, it *parses* it into a diagram and emits SVG of
 * its own construction, with `securityLevel: 'strict'` — which HTML-escapes label text and
 * refuses click handlers and script tags in the definition. React has no way to render a
 * foreign SVG document as elements, so the alternative is not a safer rendering, it is no
 * diagram at all.
 *
 * **A failed render shows the definition**, not an error. A diagram the coach wrote badly
 * is still information the learner can read, and an error box in the middle of a reply
 * reads as the app being broken (docs/06-frontend.md#markdown-in-the-transcript).
 */

import { useEffect, useState } from 'react';

import { useThemeStore } from '@/stores/theme';

/**
 * Mermaid identifies each render with a DOM id, and it must be unique on the page and
 * usable in a CSS selector. `useId()` is neither — React's ids contain `:` — so this is a
 * module-level counter instead.
 */
let nextDiagramId = 0;

export function Mermaid({ code }: { code: string }) {
  // The one thing on the page that genuinely re-renders on a theme change: mermaid bakes
  // its palette into the SVG it returns, so unlike shiki's tokens there are no variables
  // left to swap (docs/06-frontend.md#integration-points-that-are-easy-to-miss).
  const resolved = useThemeStore((state) => state.resolved);
  const [svg, setSvg] = useState<string | null>(null);
  const [id] = useState(() => `coach-mermaid-${(nextDiagramId += 1)}`);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const mermaid = (await import('mermaid')).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: resolved === 'dark' ? 'dark' : 'default',
        });
        const rendered = await mermaid.render(id, code);
        if (!cancelled) setSvg(rendered.svg);
      } catch {
        // The definition below is the fallback, so there is nothing to report.
        if (!cancelled) setSvg(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [code, id, resolved]);

  if (!svg) {
    return (
      <pre
        className="my-2 overflow-x-auto rounded-md border bg-background/60 p-3 text-xs"
        data-testid="mermaid-source"
      >
        <code className="font-mono">{code}</code>
      </pre>
    );
  }

  return (
    <div
      className="my-2 overflow-x-auto rounded-md border p-3 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-full"
      data-testid="mermaid-diagram"
      // See the module docstring: mermaid's own SVG, not the message's text.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
