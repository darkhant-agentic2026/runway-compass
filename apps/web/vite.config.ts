import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

/**
 * Tailwind v4 is wired through `@tailwindcss/vite`, not a PostCSS config — there is no
 * `tailwind.config.js` and no `postcss.config.js` in this project by design. The theme
 * lives in CSS via `@theme` in `src/index.css`.
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // Local dev only. In every deployed environment the SPA is served by the API
      // container itself, so these are same-origin and no proxy exists
      // (docs/01-architecture.md). Keep this list in step with `API_PREFIXES` in
      // `apps/api/src/coach/main.py`.
      '/api': 'http://127.0.0.1:8080',
      '/healthz': 'http://127.0.0.1:8080',
      '/readyz': 'http://127.0.0.1:8080',
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  build: {
    // The image copies this to /app/static (docs/07-infra-deploy.md#container).
    outDir: 'dist',
    sourcemap: true,
  },
})
