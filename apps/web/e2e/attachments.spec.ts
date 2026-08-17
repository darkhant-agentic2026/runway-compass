/**
 * The upload path, end to end.
 *
 * This suite exists because its absence let two defects reach a deployed environment. The
 * `InMemoryObjectStore` used to hand the browser a `https://storage.local/…` URL, so no
 * flow could complete an upload and the whole path — picker, drop zone, finalize,
 * transcript — had no end-to-end coverage at all. What got through:
 *
 * - no `<Toaster />` was mounted, so a 500 on `POST /api/uploads` produced no visible
 *   change whatsoever and looked like a dead file picker;
 * - attachments vanished from *reopened* conversations, because the transcript read
 *   `fileData` where Firestore stores `file_data`.
 *
 * Neither is exotic. The reopen assertion below is the one that matters most: everything
 * about an attachment can look right in the session that created it and still be missing
 * the next time the tab is opened, because those are two different code paths — the
 * optimistic echo, and the stored event.
 *
 * `ENV=local` serves the PUT itself (`api/routers/local_storage.py`), which is what makes
 * any of this reachable without a GCS bucket.
 */

import type { Page } from '@playwright/test'

import { expect, test } from './fixtures'

/** A one-pixel PNG, so the bytes are a real image the server will accept. */
const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=='

const PNG = { name: 'screenshot.png', mimeType: 'image/png', buffer: Buffer.from(PNG_BASE64, 'base64') }

async function openWorkspace(page: Page, projectTitle: string, taskTitle: string) {
  await page.goto('/')
  await page.getByLabel('New project').fill(projectTitle)
  await page.getByRole('button', { name: 'Create' }).click()
  await page.getByRole('link', { name: projectTitle }).click()
  await page.getByLabel('New task').fill(taskTitle)
  await page.getByRole('button', { name: 'Add task' }).click()
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click()
  await expect(page.getByTestId('transcript')).toBeVisible()
  return page.url()
}

/** Attach through the hidden file input the paperclip drives. */
async function attach(page: Page, file = PNG) {
  await page.setInputFiles('input[type="file"]', file)
  // `ready` gates sending, and the chip drops the "uploading…" suffix when finalize lands.
  await expect(page.getByTestId('attachments')).toContainText(file.name)
  await expect(page.getByTestId('attachments')).not.toContainText('uploading', {
    timeout: 20_000,
  })
}

function attachmentsIn(page: Page) {
  return page.getByTestId('transcript').getByTestId('message-attachments')
}

test('an attachment uploads, sends, and stays visible after reopening', async ({
  signedIn: page,
}) => {
  const url = await openWorkspace(page, 'Attachments', 'Read a screenshot')

  await attach(page)
  await page.getByLabel('Message your coach').fill('what do you make of this?')
  await page.getByRole('button', { name: 'Send' }).click()

  // In the session that sent it.
  await expect(attachmentsIn(page)).toHaveCount(1)
  await expect(attachmentsIn(page)).toContainText('screenshot.png')

  await expect(
    page.getByTestId('transcript').locator('[data-role="model"]').last(),
  ).toContainText('Here is what I think about', { timeout: 30_000 })

  // And after a reopen, which is a different code path: the stored event rather than the
  // optimistic echo. This is the assertion the original bug would have failed.
  await page.goto(url)
  await expect(attachmentsIn(page)).toHaveCount(1, { timeout: 20_000 })
  await expect(attachmentsIn(page)).toContainText('screenshot.png')
})

test('a reopened image attachment renders as a preview, not just a label', async ({
  signedIn: page,
}) => {
  const url = await openWorkspace(page, 'Previews', 'See the screenshot')

  await attach(page)
  await page.getByRole('button', { name: 'Send' }).click()
  await expect(attachmentsIn(page)).toHaveCount(1)

  await page.goto(url)

  // Fetched with the ID token and turned into a blob URL — an <img src> cannot carry a
  // bearer header (docs/00-overview.md keeps one auth path).
  const image = page.getByTestId('attachment-image')
  await expect(image).toBeVisible({ timeout: 20_000 })
  await expect(image).toHaveAttribute('src', /^blob:/)
  await expect(image).toHaveAttribute('alt', 'screenshot.png')
})

test('a dropped file attaches, exactly as the picker does', async ({ signedIn: page }) => {
  await openWorkspace(page, 'Dropping', 'Drop a screenshot')

  // Playwright cannot synthesise an OS drag, so the drop is dispatched with a DataTransfer
  // built in the page. That still exercises the real handlers on the chat pane, which is
  // where the bug was: they used to be on the composer strip and the drop missed them.
  const handle = await page.evaluateHandle(
    ({ base64, name }) => {
      const bytes = Uint8Array.from(atob(base64), (character) => character.charCodeAt(0))
      const transfer = new DataTransfer()
      transfer.items.add(new File([bytes], name, { type: 'image/png' }))
      return transfer
    },
    { base64: PNG_BASE64, name: PNG.name },
  )

  const pane = page.getByTestId('transcript')
  await pane.dispatchEvent('dragenter', { dataTransfer: handle })
  await expect(page.getByTestId('drop-overlay')).toBeVisible()
  await pane.dispatchEvent('drop', { dataTransfer: handle })

  await expect(page.getByTestId('attachments')).toContainText(PNG.name)
  await expect(page.getByTestId('attachments')).not.toContainText('uploading', {
    timeout: 20_000,
  })
  await expect(page.getByTestId('drop-overlay')).toBeHidden()
})

test('a refused upload says so, out loud', async ({ signedIn: page }) => {
  // The other half of what went undetected: an upload the server refuses must produce a
  // visible message. It reached production reporting through a `toast.error` with no
  // `<Toaster />` mounted, which is indistinguishable from a dead handler.
  await openWorkspace(page, 'Refusals', 'Attach the wrong thing')

  await page.setInputFiles('input[type="file"]', {
    name: 'notes.zip',
    mimeType: 'application/zip',
    buffer: Buffer.from('not an image'),
  })

  await expect(page.getByText(/cannot be attached/i)).toBeVisible({ timeout: 10_000 })
  await expect(page.getByTestId('attachments')).toHaveCount(0)
})
