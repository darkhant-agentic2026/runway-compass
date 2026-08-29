/**
 * Golden flows #6, #8, and #9 — the autonomous chain, driven from the browser.
 *
 * docs/08-testing.md:
 *
 * > 6. **Autonomous update visible on return** — trigger `/internal/tick` from the test,
 * >    assert a `board_update` arrives on an open board and the "Updated by your coach"
 * >    banner lists the change, and that undo reverses it.
 * > 8. **Presence guard** — with a client connected to project A, a tick creates no run for
 * >    A but does for project B. Then, from that same connected client, queue research on a
 * >    task in A […]: the next tick runs it, presence notwithstanding.
 * > 9. **Requested research jumps the queue** […] Asserted on the ledger rather than on the
 * >    screen: the ordering is a scheduler property.
 *
 * **`/internal/tick` is callable without OIDC here because `ENV=local`**, which is the
 * whole reason that bypass exists (docs/05-autonomous-runs.md#local-development). With
 * `ENV=local` the tick also hands each run to an in-process task rather than to Cloud
 * Tasks, so a tick from this test executes runs in the same container that served it.
 *
 * **Quiet hours are disabled first, in every test here.** The default window is 23:00–07:00
 * and it is evaluated against the wall clock, so without this the auto-scheduled half of
 * these flows passes or fails *by the hour the suite is run* — which is how the backend
 * scheduler suite first failed, at 00:44 UTC, in a way that read as a broken guard rather
 * than a working one.
 */

import type { APIRequestContext, Page } from '@playwright/test';

import { expect, test } from './fixtures';

/*
  A tick's runs are two model round trips each — the research turn and the propose pass —
  and flow #8 does two ticks. The same reasoning as the research spec: raise the describe
  timeout rather than the per-assertion waits, because an assertion given the whole test
  budget can never actually wait for it.
*/
test.describe.configure({ timeout: 90_000 });

/** The dev-token header the API honours only under `ENV=local`, for direct API calls. */
function asUser(uid: string) {
  return { Authorization: `Bearer dev:${uid}` };
}

async function disableQuietHours(request: APIRequestContext, uid: string) {
  const response = await request.patch('/api/me/prefs', {
    headers: asUser(uid),
    // `start === end` is an empty window, which is how quiet hours are turned off. See the
    // file header for why every test here needs it.
    data: { autonomousQuietHours: { start: '00:00', end: '00:00' }, timezone: 'UTC' },
  });
  expect(response.ok()).toBeTruthy();
}

async function tick(request: APIRequestContext) {
  const response = await request.post('/internal/tick');
  expect(response.ok()).toBeTruthy();
  return (await response.json()) as {
    scheduled: string[];
    recovered: string[];
    swept: number;
    skipped: Record<string, number>;
  };
}

/**
 * Wait for the runs a tick started to reach a terminal state.
 *
 * **A test that triggers background work must not leave it running.** `/internal/tick` is
 * global by design, and with `ENV=local` its runs execute in the same container — so a
 * test that ends the moment the tick returns leaves several research turns and their
 * transactions running *into the next test*, which then contends with them for the
 * emulator. That presents as `Aborted: Transaction lock timeout` on an ordinary
 * `POST /tasks` the browser had just made, in a completely unrelated test, and reads as a
 * flaky board rather than as unfinished work from three tests ago.
 *
 * Terminal covers `skipped_owner_present` as well as success and failure: a run the
 * presence guard abandoned is finished, and waiting for it to "complete" would wait
 * forever.
 */
async function settle(request: APIRequestContext, uid: string, runIds: string[]) {
  for (const runId of runIds) {
    await expect
      .poll(
        async () => {
          const response = await request.get(`/api/runs/${runId}`, { headers: asUser(uid) });
          // A run belonging to another test's user answers 404 here, which is a terminal
          // answer for our purposes: it is not ours to wait for.
          if (!response.ok()) return 'gone';
          return (await response.json()).run.status as string;
        },
        { timeout: 60_000 },
      )
      .toMatch(/^(complete|failed|cancelled|skipped_owner_present|gone)$/);
  }
}

/**
 * Resolves when this tab has actually told the server it is in `projectId`.
 *
 * **Waiting for the workspace heading is not enough**, and that is what made this flow
 * fail roughly one full-suite run in five. Presence is a WebSocket frame: the page mounts,
 * asks for a ticket, opens the socket, and only then sends `presence`
 * (`lib/socket.ts` — `handleOpen` re-sends it, so nothing is lost, but it is *later*).
 * Under a loaded suite that lag comfortably outruns the heading assertion, so the tick ran
 * against a project whose owner had not yet claimed it and scheduled a run for it. The
 * failure read as a broken presence guard and was a test racing the socket.
 *
 * Playwright can see the frame, so the test waits for the real event rather than for a
 * duration — no timeout to tune, and it asserts the exact thing the guard reads.
 *
 * Must be installed **before** the navigation that mounts the workspace.
 */
function presenceSent(page: Page, projectId: string): Promise<void> {
  return new Promise((resolve) => {
    page.on('websocket', (ws) => {
      ws.on('framesent', ({ payload }) => {
        if (typeof payload !== 'string') return;
        const frame = JSON.parse(payload) as { type?: string; projectId?: string };
        if (frame.type === 'presence' && frame.projectId === projectId) resolve();
      });
    });
  });
}

/** The runs a tick scheduled that belong to this user, newest information first. */
async function ownRuns(request: APIRequestContext, uid: string, runIds: string[]) {
  const runs = [];
  for (const runId of runIds) {
    const response = await request.get(`/api/runs/${runId}`, { headers: asUser(uid) });
    if (response.ok()) runs.push((await response.json()).run);
  }
  return runs as { id: string; projectId: string; taskId: string | null; trigger: string }[];
}

async function createProject(page: Page, title: string) {
  await page.goto('/');
  await page.getByLabel('New project').fill(title);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: title }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
}

/**
 * No minutes field: a task is sized from the project's own default at creation, the same
 * as a subtask (docs/09-roadmap.md#task-board-and-task-view-polish).
 */
async function addTask(page: Page, title: string) {
  await page.getByLabel('New task').fill(title);
  await page.getByRole('button', { name: 'Add task' }).click();
  await expect(page.getByTestId('open-workspace').filter({ hasText: title })).toBeVisible();
}

/** The id of a task on the board, from the link its card carries. */
async function taskIdOf(page: Page, title: string): Promise<string> {
  const href = await page
    .getByTestId('open-workspace')
    .filter({ hasText: title })
    .getAttribute('href');
  return href!.split('/').pop()!;
}

/** Titles of the board's cards, top to bottom. */
async function boardOrder(page: Page) {
  return page.getByTestId('open-workspace').allInnerTexts();
}

test('flow #6: a run changes the board, the banner says what it did, and undo reverses it', async ({
  signedIn: page,
  uid,
}) => {
  await disableQuietHours(page.request, uid);
  await createProject(page, 'Async Python');
  await addTask(page, 'Event loops');
  await addTask(page, 'Structured concurrency');

  const projectUrl = page.url();
  expect(await boardOrder(page)).toEqual(['Event loops', 'Structured concurrency']);

  // Queue the *second* task, so the run has something to reorder. `reprioritize` moves the
  // task whose materials are now ready to the front, which is the board change this flow
  // is about — and the one the ledger records enough to reverse.
  await page
    .getByTestId('open-workspace')
    .filter({ hasText: 'Structured concurrency' })
    .click();
  // Behind the "Manual actions" disclosure now, collapsed on load
  // (docs/09-roadmap.md#task-board-and-task-view-polish).
  await page.getByRole('button', { name: 'Manual actions' }).click();
  await page.getByTestId('queue-research').click();
  await expect(page.getByTestId('queue-research')).toHaveText(/Starts soon/);

  // Back to the board, and stay there. Everything below arrives without a reload, which is
  // what `board_update` is for.
  await page.goto(projectUrl);
  await expect(page.getByTestId('research-queued')).toBeVisible();

  const scheduled = await tick(page.request);

  // The push, from a run the browser never asked for. Before M5 the hub reached only the
  // instance that served the request; a scheduled run has no such relationship.
  await expect(page.getByTestId('materials-ready')).toBeVisible({ timeout: 60_000 });

  const banner = page.getByTestId('coach-update-banner');
  await expect(banner).toBeVisible();
  await expect(banner.getByTestId('coach-update-line')).toContainText(
    'moved “Structured concurrency” to the top',
  );
  expect(await boardOrder(page)).toEqual(['Structured concurrency', 'Event loops']);

  await banner.getByTestId('undo-run').click();

  // The order is restored *exactly*, from the `order` key the ledger recorded — a
  // fractional index cannot be inverted, so a recomputed one would land the task in the
  // right position relative to whatever the board looks like now, which is not the same
  // thing as where it was.
  await expect
    .poll(async () => await boardOrder(page))
    .toEqual(['Event loops', 'Structured concurrency']);
  // The undone run stays on screen, struck through: someone who mis-clicked needs to see
  // what they just reversed.
  await expect(banner.getByText('Undone')).toBeVisible();

  await settle(page.request, uid, scheduled.scheduled);
});

test('flow #8: presence skips auto-scheduled work, and a request runs anyway', async ({
  signedIn: page,
  uid,
}) => {
  await disableQuietHours(page.request, uid);
  await createProject(page, 'Project A');
  await addTask(page, 'Task A');
  const projectA = page.url();
  const projectAId = projectA.split('/').pop()!;
  const taskAId = await taskIdOf(page, 'Task A');
  /*
    Task A is parked before anything ticks, and put back below. It is what makes the
    barrier free of side effects: `needsResearch: false` is a real decision the product
    supports ("this task needs no prepared material"), and while it holds, project A has
    nothing for a tick to schedule — so the barrier ticks cannot accidentally do the very
    thing this test is about to assert never happens.
  */
  await page.request.patch(`/api/tasks/${taskAId}`, {
    headers: asUser(uid),
    data: { needsResearch: false },
  });
  await createProject(page, 'Project B');
  const projectBId = page.url().split('/').pop()!;

  // Open A's workspace and stay there.
  const claimed = presenceSent(page, projectAId);
  await page.goto(projectA);
  await page.getByTestId('open-workspace').filter({ hasText: 'Task A' }).click();
  await expect(page.getByRole('heading', { name: 'Task A' })).toBeVisible();
  await claimed;

  /*
    **The frame having been *sent* is not the frame having been *handled*.** `presenceSent`
    resolves on the client's `framesent`; the server writes `presence/{uid}` when its own
    socket loop gets round to it, and an HTTP tick can and does overtake that under a
    loaded suite. This barrier waits for the effect rather than for the send — the tick's
    own report is the only thing that can see `presence/{uid}`, so it is the instrument.
  */
  await expect
    .poll(async () => (await tick(page.request)).skipped.owner_present ?? 0, {
      timeout: 30_000,
    })
    .toBeGreaterThanOrEqual(1);

  // Now give both projects something to do, with presence already established.
  await page.request.patch(`/api/tasks/${taskAId}`, {
    headers: asUser(uid),
    data: { needsResearch: true },
  });
  await page.goto(`/projects/${projectBId}`);
  await addTask(page, 'Task B');
  await page.goto(projectA);
  await page.getByTestId('open-workspace').filter({ hasText: 'Task A' }).click();
  await expect(page.getByRole('heading', { name: 'Task A' })).toBeVisible();
  await page.getByRole('button', { name: 'Manual actions' }).click();

  const first = await tick(page.request);

  /*
    **Both halves, in one test.** A scheduler bug that queues everything makes B's
    assertion pass and only A's fail; one that queues nothing does the reverse. Neither
    means anything alone (docs/08-testing.md).

    Filtered to *this user's* runs: `/internal/tick` is global by design and the e2e
    database is shared with every other spec, so an assertion on the tick's total would be
    an assertion about whatever else the suite happens to be doing.
  */
  expect(first.skipped.owner_present).toBeGreaterThanOrEqual(1);
  const mine = await ownRuns(page.request, uid, first.scheduled);
  expect(mine.map((run) => run.projectId)).toEqual([projectBId]);

  // Now the half M5 changed: from this same connected client — still sitting in project A,
  // still counting as present — queue research and watch it run anyway. A guard that
  // cannot be overridden by an explicit request is a guard that refuses the one case where
  // intent is clearest.
  await page.getByTestId('queue-research').click();
  await expect(page.getByTestId('queue-research')).toHaveText(/Starts soon/);

  const second = await tick(page.request);
  expect(
    (await ownRuns(page.request, uid, second.scheduled)).map((run) => run.projectId),
  ).toEqual([projectAId]);

  await expect(page.getByTestId('checklist')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId('queue-research')).toHaveText(/Have my coach prepare this/);

  await settle(page.request, uid, [...first.scheduled, ...second.scheduled]);
});

test('flow #9: a requested run is scheduled ahead of auto-scheduled work', async ({
  signedIn: page,
  uid,
}) => {
  /*
    Asserted on the ledger, not on the screen. The ordering is a scheduler property, and a
    UI that renders both eventually cannot distinguish the orders — so this drives the real
    HTTP stack of the built image and reads the result out of the run documents.
  */
  await disableQuietHours(page.request, uid);
  for (const title of ['Auto one', 'Auto two']) {
    await createProject(page, title);
    await addTask(page, `${title} task`);
  }
  await createProject(page, 'Wanted');
  await addTask(page, 'Wanted task');
  await page.getByTestId('open-workspace').filter({ hasText: 'Wanted task' }).click();
  const wantedTaskUrl = page.url();
  await page.getByRole('button', { name: 'Manual actions' }).click();
  await page.getByTestId('queue-research').click();
  await expect(page.getByTestId('queue-research')).toHaveText(/Starts soon/);

  const result = await tick(page.request);

  /*
    Three of this user's projects have work; the requested one comes first in the list the
    tick built, and the cap is applied after that sort — which is what stops a backlog
    starving a learner who pressed a button thirty seconds ago.

    Asserted over *this user's* runs in the order the tick returned them, for the same
    reason flow #8 filters: the tick is global and the database is shared, so "the first
    run overall" is a fact about the rest of the suite.
  */
  const mine = await ownRuns(page.request, uid, result.scheduled);
  expect(mine.length).toBeGreaterThanOrEqual(1);
  expect(mine[0]!.trigger).toEqual('requested');
  expect(wantedTaskUrl).toContain(mine[0]!.taskId);
  expect(mine.slice(1).map((run) => run.trigger)).not.toContain('requested');

  await settle(page.request, uid, result.scheduled);
});
