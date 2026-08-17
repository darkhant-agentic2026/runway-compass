import { test as base, type Page } from '@playwright/test'

/**
 * Sign-in is seeded, not performed.
 *
 * docs/08-testing.md: "the app under test runs with `ENV=local`, and the Playwright
 * fixture injects a `dev:<uid>` token so every flow starts authenticated." The dev auth
 * provider reads its uid from `localStorage['coach.devUid']`, so seeding is one
 * `addInitScript` — and because the API only honours `dev:` tokens when `ENV=local`, this
 * cannot become a way in anywhere else.
 *
 * Each test gets its own uid, which is what keeps the flows independent without a
 * database reset between them: per-user isolation is enforced server-side, so a fresh uid
 * is a fresh, empty account.
 */

let counter = 0

// Unique per process, not just per test. The emulator's data outlives a single
// `playwright test` run — the compose stack stays up between runs — so a uid derived only
// from a per-process counter would hand the next run an account that already has data in
// it, and every "the board starts empty" assumption would quietly stop holding.
const RUN_ID = Math.random().toString(36).slice(2, 10)

export const test = base.extend<{ uid: string; signedIn: Page }>({
  uid: async ({}, use, testInfo) => {
    counter += 1
    await use(`u_e2e_${RUN_ID}_${testInfo.workerIndex}_${counter}`)
  },

  signedIn: async ({ page, uid }, use) => {
    await page.addInitScript(
      ({ storageUid, theme }: { storageUid: string; theme: string }) => {
        window.localStorage.setItem('coach.devUid', storageUid)
        // Pin the *initial* theme so screenshots and contrast assertions do not depend on
        // the CI machine's `prefers-color-scheme`. Only when unset: `addInitScript` runs
        // on every navigation, so writing unconditionally would undo a choice the test
        // itself made and make the reload assertion untestable.
        if (window.localStorage.getItem('coach.theme') === null) {
          window.localStorage.setItem('coach.theme', theme)
        }
      },
      { storageUid: uid, theme: 'light' },
    )
    await use(page)
  },
})

export { expect } from '@playwright/test'
