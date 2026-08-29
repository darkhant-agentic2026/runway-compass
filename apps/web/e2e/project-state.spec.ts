/**
 * Project state and archiving (docs/09-roadmap.md#project-state-and-archiving), covered
 * end-to-end because the interesting part is cross-screen: a state change made in project
 * settings has to be reflected on the main project list (paused hidden by default, behind
 * a toggle) and in a dedicated archived-projects view, and a restore from either place has
 * to bring the project back to where it started.
 */

import { expect, test } from './fixtures';

test('pausing a project hides it from the main list, behind a toggle', async ({
  signedIn: page,
}) => {
  await page.goto('/');
  await page.getByLabel('New project').fill('Pausable project');
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: 'Pausable project' }).click();

  await page.getByRole('link', { name: 'Project settings' }).click();
  await page.getByLabel('Project state').click();
  await page.getByRole('option', { name: 'Paused' }).click();
  await expect(page.getByTestId('state-explainer')).toContainText(
    "the autonomous scheduler's presence/status guard skips this project",
  );

  await page.getByRole('link', { name: '← Back to the board' }).click();
  await expect(page.getByTestId('project-state-chip')).toHaveAttribute('data-status', 'paused');

  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Pausable project' })).not.toBeVisible();

  await page.getByRole('switch', { name: 'Show paused projects' }).click();
  await expect(page.getByRole('link', { name: 'Pausable project' })).toBeVisible();
});

test('archiving a project moves it to the archived view, and restoring brings it back', async ({
  signedIn: page,
}) => {
  await page.goto('/');
  await page.getByLabel('New project').fill('Archivable project');
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: 'Archivable project' }).click();

  await page.getByRole('link', { name: 'Project settings' }).click();
  await page.getByLabel('Project state').click();
  await page.getByRole('option', { name: 'Archived' }).click();

  await page.goto('/');
  await expect(page.getByRole('link', { name: 'Archivable project' })).not.toBeVisible();
  // Not even behind the paused toggle — archived stays out of the main list entirely.
  await expect(page.getByRole('switch', { name: 'Show paused projects' })).not.toBeVisible();

  await page.getByRole('link', { name: 'Archived projects' }).click();
  await expect(page.getByRole('heading', { name: 'Archived projects' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Archivable project' })).toBeVisible();

  await page
    .getByRole('listitem')
    .filter({ hasText: 'Archivable project' })
    .getByRole('button', { name: 'Restore' })
    .click();
  await expect(page.getByText('No archived projects.')).toBeVisible();

  await page.getByRole('link', { name: '← Back to your projects' }).click();
  await expect(page.getByRole('link', { name: 'Archivable project' })).toBeVisible();
});
