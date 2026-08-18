import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end configuration.
 *
 * **Four projects, and WebKit is not optional coverage.** Golden flow #4 (disconnect and
 * resume) is an M2 exit criterion and, per docs/08-testing.md, depends on socket teardown,
 * background-tab throttling, page lifecycle, and `visibilitychange` — all four of which
 * Safari has historically differed from Chromium on. Verifying the disconnect guarantee
 * only on Chromium would verify it on the half of the population least likely to break it.
 * Mobile is a WebKit story for the same reason: on iOS every browser is WebKit, and the
 * board has explicit mobile requirements.
 *
 * The app under test runs with `ENV=local` and sign-in is seeded with a `dev:<uid>` token
 * rather than driving Google's popup (docs/08-testing.md) — no flow here is testing
 * Google's sign-in, and seeding keeps the suite fast and deterministic. The model is the
 * deterministic stub (`MODEL_BACKEND=stub` in `docker-compose.e2e.yml`), which is what
 * lets flow #4 assert character equality between an interrupted run and a control run.
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
    // Installed with `npx playwright install --with-deps webkit`; the `--with-deps` half
    // needs root, so a machine without it will fail at launch with a missing shared
    // object rather than at install time. See the module docstring for why this project
    // is load-bearing rather than extra coverage.
    { name: 'webkit', use: { ...devices['Desktop Safari'] } },
    { name: 'mobile-safari', use: { ...devices['iPhone 14'] } },
  ],
});
