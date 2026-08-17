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

import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/** Mirrors `coach.integrations.stub_model.stub_reply`. */
function stubReply(prompt: string): string {
  return (
    `Here is what I think about ${prompt}.` +
    ' Let us break it down together, one step at a time, and check your understanding' +
    ' as we go.'
  )
}

async function openWorkspace(page: Page, projectTitle: string, taskTitle: string) {
  await page.goto('/')
  await page.getByLabel('New project').fill(projectTitle)
  await page.getByRole('button', { name: 'Create' }).click()
  await page.getByRole('link', { name: projectTitle }).click()

  await page.getByLabel('New task').fill(taskTitle)
  await page.getByRole('button', { name: 'Add task' }).click()
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click()

  await expect(page.getByRole('heading', { name: taskTitle })).toBeVisible()
  await expect(page.getByTestId('transcript')).toBeVisible()
}

async function send(page: Page, message: string) {
  await page.getByLabel('Message your coach').fill(message)
  await page.getByRole('button', { name: 'Send' }).click()
}

/** The settled assistant bubble, once the turn has been handed to the transcript. */
function assistantBubbles(page: Page) {
  return page.getByTestId('transcript').locator('[data-role="model"]')
}

test('a turn streams into the transcript and settles there', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Streaming', 'Understand asyncio')

  await send(page, 'locks')

  await expect(assistantBubbles(page).last()).toHaveText(stubReply('locks'), {
    timeout: 30_000,
  })
})

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
  let armed = false
  let dropped = false
  await context.routeWebSocket(/\/ws/, (ws) => {
    const server = ws.connectToServer()
    server.onMessage((message) => {
      ws.send(message)
      // Cut once text is actually flowing. Dropping before the first delta would only
      // test reconnection; the guarantee is about losing the socket *mid-generation*.
      if (armed && !dropped && String(message).includes('"delta"')) {
        dropped = true
        ws.close()
      }
    })
    ws.onMessage((message) => server.send(message))
  })

  await openWorkspace(page, 'Disconnect', 'Survive a dropped socket')

  // --- control run: no interruption ---------------------------------------------------
  await send(page, 'control')
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('control'), {
    timeout: 30_000,
  })
  const control = await assistantBubbles(page).last().innerText()

  // --- interrupted run ------------------------------------------------------------------
  armed = true
  await send(page, 'interrupted')

  // The "still working" state has to be *visible*: a user who is told the connection
  // dropped without being told the work continues will start over, which is the wasted
  // inference the whole design exists to avoid.
  await expect(page.getByTestId('connection-banner')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('connection-banner')).toContainText('still working')

  // Then the stream continues from where it left off, over the reconnected socket.
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('interrupted'), {
    timeout: 45_000,
  })

  // The assertion the flow exists for: identical output, modulo the prompt.
  expect(await assistantBubbles(page).last().innerText()).toBe(
    control.replace('control', 'interrupted'),
  )
})

test('the transcript survives a full page reload', async ({ signedIn: page }) => {
  // Complementary to the disconnect above: that one proves the *stream* resumes, this one
  // proves the finalized events were durably written — the two halves of "inference is
  // not wasted".
  await openWorkspace(page, 'Reload', 'Persist the transcript')
  await send(page, 'durability')
  await expect(assistantBubbles(page).last()).toHaveText(stubReply('durability'), {
    timeout: 30_000,
  })

  await page.reload()

  await expect(assistantBubbles(page).last()).toHaveText(stubReply('durability'), {
    timeout: 30_000,
  })
  await expect(page.getByTestId('transcript')).toContainText('durability')
})

test('cancelling a turn stops it and says so', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Cancel', 'Stop a turn')

  await send(page, 'a very long answer please')
  await page.getByRole('button', { name: 'Cancel' }).click()

  await expect(page.getByRole('alert')).toContainText('cancelled', { timeout: 20_000 })
  // Not retryable: the user asked for this, so offering a retry would be wrong.
  await expect(page.getByRole('alert')).not.toContainText('try again')
})
