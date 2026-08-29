/**
 * Project settings' troubleshooting section (docs/09-roadmap.md's M10 status section):
 * hidden behind its own switch since it is maintenance, not preference, and "delete all
 * tasks" is a hard, irreversible delete with its own explicit confirm step — worth
 * end-to-end coverage because the interesting part is that the switch stays off by
 * default and the board is genuinely empty afterward, not just that a request went out.
 */

import { expect, test } from './fixtures';

test('delete all tasks is hidden behind a switch, confirms, and empties the board', async ({
  signedIn: page,
}) => {
  await page.goto('/');
  await page.getByLabel('New project').fill('Troubleshootable project');
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: 'Troubleshootable project' }).click();

  await page.getByLabel('New task').fill('A task to lose');
  await page.getByRole('button', { name: 'Add task' }).click();
  await expect(
    page.getByTestId('task-card').filter({ hasText: 'A task to lose' }),
  ).toBeVisible();

  await page.getByRole('link', { name: 'Project settings' }).click();
  await expect(page.getByRole('button', { name: 'Delete all tasks' })).not.toBeVisible();

  await page.getByRole('switch', { name: 'Show troubleshooting settings' }).click();
  await expect(page.getByTestId('delete-all-tasks')).toBeVisible();
  await page.getByTestId('delete-all-tasks').click();

  // The first click only asks; the board must still be untouched.
  await expect(page.getByTestId('confirm-delete-all-tasks')).toBeVisible();
  await page.getByRole('link', { name: '← Back to the board' }).click();
  await expect(
    page.getByTestId('task-card').filter({ hasText: 'A task to lose' }),
  ).toBeVisible();

  await page.getByRole('link', { name: 'Project settings' }).click();
  await page.getByRole('switch', { name: 'Show troubleshooting settings' }).click();
  await page.getByTestId('delete-all-tasks').click();
  await page.getByTestId('confirm-delete-all-tasks').click();
  await expect(page.getByText(/Deleted 1 task\.?/)).toBeVisible();

  await page.getByRole('link', { name: '← Back to the board' }).click();
  await expect(page.getByTestId('task-card')).toHaveCount(0);
});
