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

import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

async function createProject(page: Page, title: string) {
  // The `signedIn` fixture has already seeded a dev token, so this lands on the project
  // list rather than on /login.
  await page.goto('/')
  await page.getByLabel('New project').fill(title)
  await page.getByRole('button', { name: 'Create' }).click()
  await page.getByRole('link', { name: title }).click()
  await expect(page.getByRole('heading', { name: title })).toBeVisible()
}

async function addTask(page: Page, title: string, minutes = 45) {
  await page.getByLabel('New task').fill(title)
  await page.getByLabel('Minutes').fill(String(minutes))
  await page.getByRole('button', { name: 'Add task' }).click()
  await expect(page.getByTestId('task-card').filter({ hasText: title })).toBeVisible()
}

test('the SPA and the API are served from one origin', async ({ signedIn: page }) => {
  // No CORS, no rewrite layer: the same host answers both (docs/01-architecture.md).
  const spa = await page.request.get('/')
  expect(spa.status()).toBe(200)
  expect(spa.headers()['content-type']).toContain('text/html')

  const health = await page.request.get('/healthz')
  expect(health.status()).toBe(200)
  expect(await health.json()).toEqual({ status: 'ok' })

  // The catch-all must not have swallowed the API.
  const api = await page.request.get('/api/me')
  expect(api.status()).toBe(401)
  expect(api.headers()['content-type']).toContain('application/problem+json')
})

test('a deep link into a client-side route serves the SPA', async ({ signedIn: page }) => {
  // The server has no /settings route; the SPA fallback is what makes this resolve.
  await page.goto('/settings')
  await expect(page.getByRole('heading', { name: 'Settings' })).toBeVisible()
})

test('an unauthenticated visitor is sent to the sign-in screen', async ({ page }) => {
  // Deliberately the bare `page`, with no seeded token: this is the one test that
  // exercises the route guard's signed-out branch.
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('button', { name: 'Continue as the local dev user' })).toBeVisible()
})

test('sign in, create a project, and see it on the list', async ({ page }) => {
  await page.goto('/login')
  await page.getByRole('button', { name: 'Continue as the local dev user' }).click()

  await expect(page.getByRole('heading', { name: 'Your projects' })).toBeVisible()
  await page.getByLabel('New project').fill('Learn Rust')
  await page.getByRole('button', { name: 'Create' }).click()

  await expect(page.getByTestId('project-list')).toContainText('Learn Rust')
})

test('manage tasks by hand: add, start, complete, and the default filter hides it', async ({
  signedIn: page,
}) => {
  await createProject(page, 'Manage by hand')

  await addTask(page, 'First task', 30)
  await addTask(page, 'Second task', 90)
  await expect(page.getByTestId('task-card')).toHaveCount(2)

  // Start it: only legal transitions are offered, so "Complete" is not there yet.
  await page.getByRole('button', { name: 'Actions for First task' }).click()
  await expect(page.getByRole('menuitem', { name: 'Complete' })).toHaveCount(0)
  await page.getByRole('menuitem', { name: 'Start' }).click()

  const first = page.getByTestId('task-card').filter({ hasText: 'First task' })
  await expect(first).toHaveAttribute('data-state', 'current')
  await expect(first).toContainText('Next up')

  await page.getByRole('button', { name: 'Actions for First task' }).click()
  await page.getByRole('menuitem', { name: 'Complete' }).click()

  // "Hide completed" is on by default, so the finished task leaves the board.
  await expect(page.getByTestId('task-card')).toHaveCount(1)
  await expect(page.getByTestId('board')).not.toContainText('First task')

  await page.getByRole('switch', { name: 'Hide completed' }).click()
  await expect(page.getByTestId('board')).toContainText('First task')
})

test('reordering with the keyboard fallback survives a reload', async ({
  signedIn: page,
}) => {
  // The reorder is optimistic and computes the fractional index client-side. Reloading
  // proves the server independently agreed on the same position, which is the whole
  // point of sharing the algorithm.
  await createProject(page, 'Ordering')
  await addTask(page, 'Alpha')
  await addTask(page, 'Beta')
  await addTask(page, 'Gamma')

  const titles = () =>
    page.getByTestId('task-card').evaluateAll((cards) =>
      cards.map((card) => card.querySelector('.font-medium')?.textContent?.trim() ?? ''),
    )

  expect(await titles()).toEqual(['Alpha', 'Beta', 'Gamma'])

  await page.getByRole('button', { name: 'Actions for Gamma' }).click()
  await page.getByRole('menuitem', { name: 'Move up' }).click()
  await expect.poll(titles).toEqual(['Alpha', 'Gamma', 'Beta'])

  await page.reload()
  await expect.poll(titles).toEqual(['Alpha', 'Gamma', 'Beta'])
})

test('a split parent shows its subtask count and summed duration', async ({
  signedIn: page,
}) => {
  await createProject(page, 'Splitting')
  await addTask(page, 'Big thing', 120)

  await page.getByRole('button', { name: 'Actions for Big thing' }).click()
  await page.getByRole('menuitem', { name: 'Split into subtasks…' }).click()

  const parent = page.getByTestId('task-card').filter({ hasText: 'Big thing' })
  await expect(parent.getByTestId('rollup')).toContainText('2 subtasks')
  await expect(parent.getByTestId('rollup')).toContainText('2 h')
  await expect(parent.getByTestId('subtask')).toHaveCount(2)
})

test('project preferences override the global default', async ({ signedIn: page }) => {
  // The brief's example, end to end: 45 minutes globally, 2 hours in this project.
  await createProject(page, 'Overrides')
  await page.getByRole('link', { name: 'Project settings' }).click()

  await expect(page.getByTestId('minutes-explainer')).toContainText('Inheriting 45 min')

  await page.getByLabel('Default task length (minutes)').fill('120')
  await page.getByLabel('Default task length (minutes)').blur()

  await expect(page.getByTestId('minutes-explainer')).toContainText('in effect: 2 h')
  await expect(page.getByTestId('minutes-explainer')).toContainText('Overriding')
})

test('the theme choice survives a reload and is applied before paint', async ({
  signedIn: page,
}) => {
  await page.goto('/')
  await page.getByRole('link', { name: 'Settings' }).click()

  await page.getByRole('button', { name: 'Dark' }).click()
  await expect(page.locator('html')).toHaveClass(/dark/)
  await expect(page.getByTestId('theme-explainer')).toContainText('Always dark')

  await page.reload()
  // The class is set by the inline script before React mounts, so it is already present
  // on the very first evaluation after navigation.
  await expect(page.locator('html')).toHaveClass(/dark/)
  expect(await page.evaluate(() => document.documentElement.style.colorScheme)).toBe('dark')
})
