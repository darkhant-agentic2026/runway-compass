import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    globals: true,
    // `build`/`gcp-build` compile everything under src/, tests included, into lib/ (see
    // tsconfig.json) — without this, vitest's default glob picks up the compiled
    // lib/*.test.js too and fails trying to run it as CommonJS.
    exclude: ['node_modules/**', 'lib/**'],
  },
});
