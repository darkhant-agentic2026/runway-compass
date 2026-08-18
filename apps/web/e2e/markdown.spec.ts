/**
 * The coach's markdown, rendered in a built bundle.
 *
 * `Markdown.test.tsx` covers the decisions — which fence becomes a diagram, what survives
 * a failed load, what never becomes markup — and it mocks shiki and mermaid to do it,
 * because a unit test that let the real ones load would be asserting things about
 * someone else's Python grammar.
 *
 * What that leaves unproven is the half that only exists in a build: both libraries are
 * reached through `import()`, so whether their chunks are emitted, served, and resolved
 * at runtime is a question no vitest run can answer. It is the shape of defect
 * docs/09-roadmap.md#what-a-green-local-run-does-not-prove keeps a table of — green
 * locally, broken where it matters — and this spec is the answer to it. The e2e stack
 * serves a *built* SPA, so a highlighted token here means the chunk really arrived.
 *
 * The prompt is the one phrase `stub_model.MARKDOWN_REPLY` answers in markdown.
 */

import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

async function openWorkspace(page: Page, projectTitle: string, taskTitle: string) {
  await page.goto('/');
  await page.getByLabel('New project').fill(projectTitle);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: projectTitle }).click();

  await page.getByLabel('New task').fill(taskTitle);
  await page.getByRole('button', { name: 'Add task' }).click();
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click();
  await expect(page.getByTestId('transcript')).toBeVisible();
}

test('a reply renders as tables, equations, highlighted code, and a diagram', async ({
  signedIn: page,
}) => {
  await openWorkspace(page, 'Markdown', 'Render the coach');

  await page.getByLabel('Message your coach').fill('show me the formatting');
  await page.getByRole('button', { name: 'Send' }).click();

  const bubble = page.getByTestId('transcript').locator('[data-role="model"]').last();

  // Mermaid first, and that ordering is the streaming rule showing through rather than a
  // preference: while the turn generates, the diagram is deliberately still a fenced
  // block — and shiki has a `mermaid` grammar, so *two* blocks are highlighted for those
  // couple of seconds. Waiting for the diagram is waiting for the turn to have settled.
  // The failure path renders `mermaid-source` instead, so this cannot pass on it.
  await expect(bubble.getByTestId('mermaid-diagram').locator('svg')).toBeVisible();

  // GFM: the table is a table, not four lines of pipes.
  await expect(bubble.getByRole('cell', { name: 'Read the paper' })).toBeVisible();

  // KaTeX: the `$…$` is gone and the equation is in KaTeX's own container.
  await expect(bubble.locator('.katex').first()).toBeVisible();
  await expect(bubble).not.toContainText('$O(n');

  // Shiki: `code.shiki` only appears once the highlighter chunk has loaded *and*
  // tokenized, so this is the assertion that the dynamic import resolves in a build.
  await expect(bubble.locator('pre[data-lang="python"] code.shiki')).toBeVisible();
  await expect(bubble.getByTestId('code-block')).toContainText('def merge(left, right):');
});

test('the diagram follows the theme, and the code does so without re-rendering', async ({
  signedIn: page,
}) => {
  await openWorkspace(page, 'Markdown themes', 'Render in the dark');

  await page.getByLabel('Message your coach').fill('show me the formatting');
  await page.getByRole('button', { name: 'Send' }).click();

  const bubble = page.getByTestId('transcript').locator('[data-role="model"]').last();
  await expect(bubble.getByTestId('mermaid-diagram').locator('svg')).toBeVisible();
  await expect(bubble.locator('pre[data-lang="python"] code.shiki')).toBeVisible();

  // Every token carries both colours at once, which is what makes the theme switch one
  // class on <html> rather than a re-highlight (docs/06-frontend.md).
  const style = await bubble
    .locator('pre[data-lang="python"] code.shiki span[style]')
    .first()
    .getAttribute('style');
  expect(style).toContain('--shiki-light');
  expect(style).toContain('--shiki-dark');

  await page.goto('/settings');
  await page.getByRole('button', { name: 'Dark' }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);

  await page.goBack();
  // The diagram is the one thing that has to be rebuilt — mermaid bakes its palette into
  // the SVG — so what is asserted is that it survives the switch rather than vanishing.
  await expect(bubble.getByTestId('mermaid-diagram').locator('svg')).toBeVisible();
});
