import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end configuration.
 *
 * **Chromium only through M1.** docs/07-infra-deploy.md and docs/08-testing.md: WebKit is
 * first *required* at M2, where golden flow #4 (disconnect and resume) becomes an exit
 * criterion, and installing it needs `--with-deps`, which needs sudo. The WebKit project
 * below is therefore written out and commented rather than deleted, so enabling it at M2
 * is a one-line change and the reasoning does not have to be rediscovered.
 *
 * The app under test runs with `ENV=local` and sign-in is seeded with a `dev:<uid>` token
 * rather than driving Google's popup (docs/08-testing.md) — no flow here is testing
 * Google's sign-in, and seeding keeps the suite fast and deterministic.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },

  use: {
    // The docker-compose harness serves the SPA and the API from one origin, exactly as
    // Cloud Run does (docs/01-architecture.md).
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8080',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    {
      name: 'mobile-chrome',
      use: { ...devices['Pixel 7'] },
    },
    // --- M2 -----------------------------------------------------------------------
    // WebKit is not optional coverage: on iOS every browser is WebKit and the board has
    // explicit mobile requirements, and the disconnect guarantee depends on socket
    // teardown, background-tab throttling, page lifecycle, and `visibilitychange` —
    // all four of which Safari has historically differed from Chromium on.
    //
    //   npx playwright install --with-deps webkit
    //
    // { name: 'webkit', use: { ...devices['Desktop Safari'] } },
    // { name: 'mobile-safari', use: { ...devices['iPhone 14'] } },
  ],
})
