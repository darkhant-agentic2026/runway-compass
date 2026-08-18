import '@testing-library/jest-dom/vitest'

import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, vi } from 'vitest'

/**
 * jsdom implements no `matchMedia`, and the theme code is built around it. A controllable
 * fake is installed here so every test can decide what the OS "prefers" — the theme
 * matrix in docs/08-testing.md is exactly `pref` x `prefers-color-scheme`.
 */
type Listener = (event: MediaQueryListEvent) => void

const listeners = new Set<Listener>()
let prefersDark = false

export function setPrefersDark(next: boolean): void {
  prefersDark = next
  const event = { matches: next, media: '(prefers-color-scheme: dark)' } as MediaQueryListEvent
  listeners.forEach((listener) => listener(event))
}

beforeEach(() => {
  prefersDark = false
  listeners.clear()
  vi.stubGlobal(
    'matchMedia',
    (query: string) =>
      ({
        matches: query.includes('dark') ? prefersDark : false,
        media: query,
        onchange: null,
        addEventListener: (_type: string, listener: Listener) => listeners.add(listener),
        removeEventListener: (_type: string, listener: Listener) => listeners.delete(listener),
        addListener: (listener: Listener) => listeners.add(listener),
        removeListener: (listener: Listener) => listeners.delete(listener),
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  )
})

afterEach(() => {
  cleanup()
  localStorage.clear()
  document.documentElement.className = ''
  document.documentElement.style.colorScheme = ''
  vi.unstubAllGlobals()
})
