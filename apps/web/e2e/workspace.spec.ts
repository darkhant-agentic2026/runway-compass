/**
 * Golden flow #4 — disconnect and resume.
 *
 * docs/08-testing.md calls this *the highest-value e2e test in the suite*, and says why:
 * it verifies the one guarantee that is invisible from the UI when it works and
 * catastrophic when it silently doesn't. The shape it prescribes:
 *
 * > start a turn, kill the WebSocket mid-stream with `page.routeWebSocket()`, confirm the
 * > "still working" UI, let the socket reconnect, assert the completed message is
 * > identical to the un-interrupted control run.
 *
 * **`routeWebSocket`, not a CDP session.** `newCDPSession()` is Chromium-only and throws
 * on WebKit — so specifying this flow in terms of CDP would make it unrunnable on the
 * exact engine it most needs to run on. `routeWebSocket` works on both engines and is
 * also the more precise instrument: it drops the socket while leaving REST alive, which
 * is the scenario under test. `context.setOffline()` would also kill `POST /turns` and
 * the transcript refetch, so a passing test would prove less.
 *
 * The model is the deterministic stub (`MODEL_BACKEND=stub`), so "identical to the
 * control run" is a character-for-character assertion rather than a similarity check.
 */

import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

/** Mirrors `coach.integrations.stub_model.stub_reply`. */
function stubReply(prompt: string): string {
  return (
    `Here is what I think about ${prompt}.` +
    ' Let us break it down together, one step at a time, and check your understanding' +
    ' as we go.'
  );
}

async function openWorkspace(page: Page, projectTitle: string, taskTitle: string) {
  await page.goto('/');
  await page.getByLabel('New project').fill(projectTitle);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: projectTitle }).click();

  await page.getByLabel('New task').fill(taskTitle);
  await page.getByRole('button', { name: 'Add task' }).click();
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click();

  await expect(page.getByRole('heading', { name: taskTitle })).toBeVisible();
  await expect(page.getByTestId('transcript')).toBeVisible();
}

async function send(page: Page, message: string) {
  await page.getByLabel('Message your coach').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
}

/** The settled assistant bubble, once the turn has been handed to the transcript. */
function assistantBubbles(page: Page) {
  return page.getByTestId('transcript').locator('[data-role="model"]');
}

test('a turn streams into the transcript and settles there', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Streaming', 'Understand asyncio');

  await send(page, 'locks');

  await expect(assistantBubbles(page).last()).toHaveText(stubReply('locks'), {
    timeout: 30_000,
  });
});

test('the sender sees their own message immediately, not when the reply lands', async ({
  signedIn: page,
}) => {
  // The reply takes a couple of seconds, and for that whole time the transcript used to
  // show only "Your coach is thinking…" — no record of what had been asked. ADK writes
  // the user event during generation and the transcript is refetched on `turn_complete`,
  // so without an optimistic echo the sender's own message is invisible until the end.
  await openWorkspace(page, 'Echo', 'See my own message');

  await send(page, 'is this visible yet?');

  const own = page.getByTestId('transcript').locator('[data-role="user"]');
  // Asserted *before* any assistant text exists, which is the whole point.
  await expect(own.last()).toHaveText('is this visible yet?', { timeout: 5_000 });
  await expect(page.getByTestId('still-working')).toBeVisible();

  // And it survives the handoff: the echo is replaced by the stored event, not duplicated.
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('is this visible yet?'), {
    timeout: 30_000,
  });
  await expect(own).toHaveCount(1);
});

test('the sent message survives a reload, once', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Echo reload', 'Persist my own message');
  await send(page, 'remember this');
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('remember this'), {
    timeout: 30_000,
  });

  await page.reload();

  const own = page.getByTestId('transcript').locator('[data-role="user"]');
  await expect(own).toHaveCount(1);
  await expect(own.first()).toHaveText('remember this');
});

test('a disconnect mid-stream resumes and produces the identical message', async ({
  signedIn: page,
  context,
}) => {
  // The route has to be in place *before* the page opens its socket, which happens as
  // soon as the app shell mounts. Registering it after navigating would leave the live
  // connection un-proxied, and nothing would ever be dropped.
  //
  // `armed` keeps the control run honest: both runs go through the same proxy, and only
  // the second one is cut. `dropped` makes the cut happen exactly once, so the reconnect
  // reaches the real server.
  let armed = false;
  let dropped = false;
  await context.routeWebSocket(/\/ws/, (ws) => {
    const server = ws.connectToServer();
    server.onMessage((message) => {
      ws.send(message);
      // Cut once text is actually flowing. Dropping before the first delta would only
      // test reconnection; the guarantee is about losing the socket *mid-generation*.
      if (armed && !dropped && String(message).includes('"delta"')) {
        dropped = true;
        ws.close();
      }
    });
    ws.onMessage((message) => server.send(message));
  });

  // Hold the *reconnect's* ticket, so the reconnecting window is a duration this test
  // chose rather than one it races. Left alone it is `backoffDelay(0)` — a random
  // 0-500 ms — plus a local round trip, which on a fast machine is regularly shorter than
  // one polling interval: the banner renders, the assertion misses it, and flow #4 fails
  // intermittently on whichever browser happened to be quickest. An intermittent version
  // of the suite's highest-value test is worse than none, because the response to it is
  // to re-run rather than to look.
  //
  // It also makes the disconnect *mean* something: generation carries on server-side for
  // a second and a half with nobody attached, which is the guarantee under test rather
  // than a few milliseconds of it.
  await page.route('**/api/ws-ticket', async (route) => {
    if (armed) await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.continue();
  });

  await openWorkspace(page, 'Disconnect', 'Survive a dropped socket');

  // --- control run: no interruption ---------------------------------------------------
  await send(page, 'control');
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('control'), {
    timeout: 30_000,
  });
  const control = await assistantBubbles(page).last().innerText();

  // --- interrupted run ------------------------------------------------------------------
  armed = true;
  await send(page, 'interrupted');

  // The "still working" state has to be *visible*: a user who is told the connection
  // dropped without being told the work continues will start over, which is the wasted
  // inference the whole design exists to avoid.
  await expect(page.getByTestId('connection-banner')).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId('connection-banner')).toContainText('still working');

  // Then the stream continues from where it left off, over the reconnected socket.
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('interrupted'), {
    timeout: 45_000,
  });

  // The assertion the flow exists for: identical output, modulo the prompt.
  expect(await assistantBubbles(page).last().innerText()).toBe(
    control.replace('control', 'interrupted'),
  );
});

test('the transcript survives a full page reload', async ({ signedIn: page }) => {
  // Complementary to the disconnect above: that one proves the *stream* resumes, this one
  // proves the finalized events were durably written — the two halves of "inference is
  // not wasted".
  await openWorkspace(page, 'Reload', 'Persist the transcript');
  await send(page, 'durability');
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('durability'), {
    timeout: 30_000,
  });

  await page.reload();

  await expect(assistantBubbles(page).last()).toHaveText(stubReply('durability'), {
    timeout: 30_000,
  });
  await expect(page.getByTestId('transcript')).toContainText('durability');
});

test('cancelling a turn stops it and says so', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Cancel', 'Stop a turn');

  // The stub echoes the prompt back, so a long prompt is a long reply: at 40 ms a chunk
  // this leaves several seconds of generation to cancel *into*, rather than a window the
  // click has to hit.
  const prompt = Array.from({ length: 60 }, (_, index) => `word${index}`).join(' ');
  await send(page, prompt);

  // Wait until it is demonstrably in flight, so the click cannot race the 202 that
  // registers the turn — until then the composer still shows Send.
  await expect(page.getByTestId('live-turn')).toContainText('Here is what I think about');
  await page.getByRole('button', { name: 'Cancel' }).click();

  await expect(page.getByRole('alert')).toContainText('cancelled', { timeout: 20_000 });
  // Not retryable: the user asked for this, so offering a retry would be wrong.
  await expect(page.getByRole('alert')).not.toContainText('try again');
});

test('a failed turn does not block the next one', async ({ signedIn: page }) => {
  /*
    Found on `coach-dev`: Vertex answered a turn with 429, the error rendered in red, and
    the next message the learner sent never appeared. It had generated — a reload showed
    it — but the pane was still rendering the *failed* turn, because a turn that ends in
    `turn_error` is never cleared from `useStreamStore` and the pane read the first
    buffered turn for the session rather than the newest.

    Two changes fix it — `newestTurnFor` reads the newest buffer instead of the first, and
    `begin` retires a failed one — and **either alone makes this spec pass**, verified by
    reverting each in turn. So this is a regression test for the *symptom*, not evidence
    about which mechanism carries it; `stream.test.ts` pins the two separately. What it
    does prove is the thing no unit test can: that the pane, the composer, and the
    transcript all move on, in a built bundle, without a reload.
  */
  await openWorkspace(page, 'Error recovery', 'A task that errors');

  await send(page, 'make this turn fail');

  const error = page.getByTestId('turn-error');
  await expect(error).toBeVisible();
  await expect(error).toContainText('You can try again');

  // No reload, no retry button — just the next ordinary message.
  await send(page, 'hello again');

  await expect(error).toBeHidden();
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('hello again'), {
    timeout: 30_000,
  });
});
