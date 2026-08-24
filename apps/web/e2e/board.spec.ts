/**
 * M1 end-to-end coverage: the board works as a plain CRUD app.
 *
 * The eight golden flows in docs/08-testing.md all need the agent, sessions, or
 * autonomous runs, so none of them is runnable before M2. What *is* checkable now is the
 * M1 exit criterion — "a user can manage projects and tasks entirely by hand" — plus the
 * two M1 behaviours that are invisible from a unit test: that the SPA and the API are
 * served from one origin, and that a reorder survives a reload (i.e. the optimistic key
 * really was the server's).
 */

import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

async function createProject(page: Page, title: string) {
  // The `signedIn` fixture has already seeded a dev token, so this lands on the project
  // list rather than on /login.
  await page.goto('/');
  await page.getByLabel('New project').fill(title);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: title }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
}

async function addTask(page: Page, title: string, minutes = 45) {
  await page.getByLabel('New task').fill(title);
  await page.getByLabel('Minutes').fill(String(minutes));
  await page.getByRole('button', { name: 'Add task' }).click();
  await expect(page.getByTestId('task-card').filter({ hasText: title })).toBeVisible();
}

/**
 * Open a task's workspace and add subtasks to it, one at a time.
 *
 * The only hand path to a subtask since `POST /api/tasks/{id}/split` was removed, and it
 * lives on the workspace rather than the board because that is the screen a composite task
 * is worked from. Leaves the page *on* the workspace, which is where both callers want it.
 */
async function addSubtasks(page: Page, taskTitle: string, subtasks: [string, number][]) {
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click();
  await expect(page.getByTestId('add-subtask')).toBeVisible();

  for (const [title, minutes] of subtasks) {
    await page.getByLabel('New subtask').fill(title);
    await page.getByLabel('Minutes').fill(String(minutes));
    await page.getByRole('button', { name: 'Add subtask' }).click();
    await expect(page.getByTestId('subtask-card').filter({ hasText: title })).toBeVisible();
  }
}

test('the SPA and the API are served from one origin', async ({ signedIn: page }) => {
  // No CORS, no rewrite layer: the same host answers both (docs/01-architecture.md).
  const spa = await page.request.get('/');
  expect(spa.status()).toBe(200);
  expect(spa.headers()['content-type']).toContain('text/html');

  const health = await page.request.get('/livez');
  expect(health.status()).toBe(200);
  expect(await health.json()).toEqual({ status: 'ok' });

  // The catch-all must not have swallowed the API.
  const api = await page.request.get('/api/me');
  expect(api.status()).toBe(401);
  expect(api.headers()['content-type']).toContain('application/problem+json');
});

test('a deep link into a client-side route serves the SPA', async ({ signedIn: page }) => {
  // The server has no /settings route; the SPA fallback is what makes this resolve.
  await page.goto('/settings');
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible();
});

test('an unauthenticated visitor is sent to the sign-in screen', async ({ page }) => {
  // Deliberately the bare `page`, with no seeded token: this is the one test that
  // exercises the route guard's signed-out branch.
  await page.goto('/');
  await expect(page).toHaveURL(/\/login$/);
  await expect(
    page.getByRole('button', { name: 'Continue as the local dev user' }),
  ).toBeVisible();
});

test('sign in, create a project, and see it on the list', async ({ page }) => {
  await page.goto('/login');
  await page.getByRole('button', { name: 'Continue as the local dev user' }).click();

  await expect(page.getByRole('heading', { name: 'Your projects' })).toBeVisible();

  // M0's exit criterion: the signed-in user's own identity round-trips through the
  // server. It comes from `GET /api/me`, so asserting on it proves the token
  // round-tripped through `verify_id_token` rather than merely that the client thinks
  // it signed in. The visible text is the display name (the dev auth provider sets it
  // to the uid, so it is non-empty but has no "@"); the email — proof this came from
  // the server and not just local state — is on the hover title.
  const identity = page.getByTestId('signed-in-identity');
  await expect(identity).not.toBeEmpty();
  await expect(identity).toHaveAttribute('title', /@/);
  await page.getByLabel('New project').fill('Learn Rust');
  await page.getByRole('button', { name: 'Create' }).click();

  await expect(page.getByTestId('project-list')).toContainText('Learn Rust');
});

test('manage tasks by hand: add, start, complete, and the default filter hides it', async ({
  signedIn: page,
}) => {
  await createProject(page, 'Manage by hand');

  await addTask(page, 'First task', 30);
  await addTask(page, 'Second task', 90);
  await expect(page.getByTestId('task-card')).toHaveCount(2);

  // Only legal transitions are offered: a task with no plan yet cannot be postponed,
  // because deferring is reachable only from `in_progress`.
  await page.getByRole('button', { name: 'Actions for First task' }).click();
  await expect(page.getByRole('menuitem', { name: 'Postpone' })).toHaveCount(0);
  await page.getByRole('menuitem', { name: 'Start' }).click();

  const first = page.getByTestId('task-card').filter({ hasText: 'First task' });
  await expect(first).toHaveAttribute('data-state', 'in_progress');
  await expect(first).toContainText('Next up');

  await page.getByRole('button', { name: 'Actions for First task' }).click();
  await page.getByRole('menuitem', { name: 'Complete' }).click();

  // "Hide completed" is on by default, so the finished task leaves the board.
  await expect(page.getByTestId('task-card')).toHaveCount(1);
  await expect(page.getByTestId('board')).not.toContainText('First task');

  await page.getByRole('switch', { name: 'Hide completed' }).click();
  await expect(page.getByTestId('board')).toContainText('First task');
});

test('reordering with the keyboard fallback survives a reload', async ({ signedIn: page }) => {
  // The reorder is optimistic and computes the fractional index client-side. Reloading
  // proves the server independently agreed on the same position, which is the whole
  // point of sharing the algorithm.
  await createProject(page, 'Ordering');
  await addTask(page, 'Alpha');
  await addTask(page, 'Beta');
  await addTask(page, 'Gamma');

  const titles = () =>
    page
      .getByTestId('task-card')
      .evaluateAll((cards) =>
        cards.map((card) => card.querySelector('.font-medium')?.textContent?.trim() ?? ''),
      );

  expect(await titles()).toEqual(['Alpha', 'Beta', 'Gamma']);

  // Held from before the click, because the point of the assertion after it is that the
  // *optimistic* order is on screen while this is still in flight.
  const reordered = page.waitForResponse(
    (response) => response.url().includes('/reorder') && response.request().method() === 'POST',
  );

  await page.getByRole('button', { name: 'Actions for Gamma' }).click();
  await page.getByRole('menuitem', { name: 'Move up' }).click();
  await expect.poll(titles).toEqual(['Alpha', 'Gamma', 'Beta']);

  // And reloading before it lands would prove nothing — worse, it would *cause* the
  // failure it looks like it found: a navigation cancels in-flight requests, so the
  // reorder would never reach the server and the reloaded board would honestly show the
  // original order. Optimistic mutations make that window invisible from the UI, which
  // is why waiting on the response is the only way to be sure which order is being read
  // back. Seen once as a one-in-nine failure on webkit before this line existed.
  await reordered;

  await page.reload();
  await expect.poll(titles).toEqual(['Alpha', 'Gamma', 'Beta']);
});

test('a composite parent shows its subtask count and summed duration', async ({
  signedIn: page,
}) => {
  // Subtasks are added one at a time from the workspace now. The board's "Split into
  // subtasks…" row action and `POST /api/tasks/{id}/split` behind it were removed after
  // M4: one call producing a whole breakdown made the model commit to every piece before
  // discussing any of them, and there is no sensible hand version of that either.
  await createProject(page, 'Breaking up');
  await addTask(page, 'Big thing', 120);
  await addSubtasks(page, 'Big thing', [
    ['First half', 60],
    ['Second half', 60],
  ]);

  await page.getByRole('link', { name: 'Back to the board' }).click();
  const parent = page.getByTestId('task-card').filter({ hasText: 'Big thing' });
  await expect(parent.getByTestId('rollup')).toContainText('2 subtasks');
  await expect(parent.getByTestId('rollup')).toContainText('2 h');
  await expect(parent.getByTestId('subtask')).toHaveCount(2);
});

test('project preferences override the global default', async ({ signedIn: page }) => {
  // The brief's example, end to end: 45 minutes globally, 2 hours in this project.
  await createProject(page, 'Overrides');
  await page.getByRole('link', { name: 'Project settings' }).click();

  await expect(page.getByTestId('minutes-explainer')).toContainText('Inheriting 45 min');

  await page.getByLabel('Default task length (minutes)').fill('120');
  await page.getByLabel('Default task length (minutes)').blur();

  await expect(page.getByTestId('minutes-explainer')).toContainText('in effect: 2 h');
  await expect(page.getByTestId('minutes-explainer')).toContainText('Overriding');
});

test('the theme choice survives a reload and is applied before paint', async ({
  signedIn: page,
}) => {
  await page.goto('/');
  await page.getByRole('link', { name: 'Settings' }).click();

  await page.getByRole('button', { name: 'Dark' }).click();
  await expect(page.locator('html')).toHaveClass(/dark/);
  await expect(page.getByTestId('theme-explainer')).toContainText('Always dark');

  await page.reload();
  // The class is set by the inline script before React mounts, so it is already present
  // on the very first evaluation after navigation.
  await expect(page.locator('html')).toHaveClass(/dark/);
  expect(await page.evaluate(() => document.documentElement.style.colorScheme)).toBe('dark');
});

test('a composite task shows its subtasks in the workspace, and completing one lands on the board', async ({
  signedIn: page,
}) => {
  // The half of the split that the board test above cannot see: the task's own screen.
  // `GET /api/tasks/{id}` has always returned `subtasks[]`, so this is about the wiring —
  // the cards, the fact that none of them navigates, and the mutation reaching both the
  // detail query the workspace reads and the board query it does not.
  await createProject(page, 'Composite');
  await addTask(page, 'Big piece', 120);
  await addSubtasks(page, 'Big piece', [
    ['First half', 60],
    ['Second half', 60],
  ]);

  const cards = page.getByTestId('subtask-cards').getByTestId('subtask-card');
  await expect(cards).toHaveCount(2);
  await expect(page.getByTestId('subtask-rollup')).toContainText('0 of 2 subtasks done');
  // A subtask has no workspace of its own, so nothing among the cards is a link — the
  // way back to the board is outside them.
  await expect(page.getByTestId('subtask-cards').getByRole('link')).toHaveCount(0);

  // `not_started` → `current` → `completed`: the quick action offers whichever is legal,
  // which is the reason it is not a bare checkbox.
  await cards.first().getByRole('button', { name: 'Start' }).click();
  await cards.first().getByRole('button', { name: 'Complete' }).click();
  await expect(page.getByTestId('subtask-rollup')).toContainText('1 of 2 subtasks done');

  await page.getByRole('link', { name: 'Back to the board' }).click();
  const parent = page.getByTestId('task-card').filter({ hasText: 'Big piece' });
  await expect(parent.getByTestId('rollup')).toContainText('2 subtasks');
  await expect(parent.getByTestId('subtask')).toHaveCount(1);
});

test('the workspace shows breadcrumbs and a collapsible detail column', async ({
  signedIn: page,
}) => {
  await createProject(page, 'Breadcrumb trail');
  await addTask(page, 'Read the paper', 30);
  await page.getByTestId('open-workspace').filter({ hasText: 'Read the paper' }).click();

  const breadcrumb = page.getByRole('navigation', { name: 'Breadcrumb' });
  await expect(breadcrumb.getByRole('link', { name: 'Breadcrumb trail' })).toBeVisible();
  await expect(breadcrumb.getByText('Read the paper')).toBeVisible();

  // The narrow info strip — no title, since the breadcrumb already carries it.
  const strip = page.getByTestId('task-info-strip');
  await expect(strip).toContainText('30 min');
  await expect(strip).toContainText('No plan yet');
  await expect(strip).not.toContainText('Read the paper');

  // Expanded by default: nothing collapses on its own.
  const details = page.getByRole('region', { name: 'Task details' });
  await expect(details).toBeVisible();

  const toggle = page.getByTestId('toggle-task-details');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await toggle.click();
  await expect(details).toBeHidden();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');

  // The chat pane takes the room the detail column gave up rather than leaving it empty.
  await expect(page.getByLabel('Message your coach')).toBeVisible();

  // A reload keeps the learner's choice — it is remembered per task, not per session.
  await page.reload();
  await expect(page.getByRole('region', { name: 'Task details' })).toBeHidden();

  await page.getByTestId('toggle-task-details').click();
  await expect(page.getByRole('region', { name: 'Task details' })).toBeVisible();
});
