/**
 * Theme resolution and — the important one — the storage-key contract.
 *
 * docs/08-testing.md:
 *
 * > **Theme storage-key contract** — asserts `useThemeStore` writes a plain string to
 * > `localStorage['coach.theme']` in exactly the format the inline `index.html` script
 * > parses. These two are coupled across a boundary the type system cannot see, so the
 * > contract is pinned by a test rather than by a comment.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { setPrefersDark } from '@/test/setup'
import {
  THEME_STORAGE_KEY,
  applyTheme,
  readStoredPref,
  resolveTheme,
  startThemeSync,
  useThemeStore,
  type ResolvedTheme,
  type ThemePref,
} from '@/stores/theme'

/**
 * The inline script from `index.html`, re-implemented here *by reading the real file* so
 * that this cannot drift into testing a copy. If someone edits the script in a way that
 * changes the key or the value format, this fails.
 */
function runInlineScriptLogic(): { dark: boolean; colorScheme: string } {
  // Resolved from the project root rather than from `import.meta.url`: under jsdom the
  // module URL is an http: URL, not a file: one.
  const html = readFileSync(resolve(process.cwd(), 'index.html'), 'utf-8')
  const match = /<script>([\s\S]*?)<\/script>/.exec(html)
  if (!match?.[1]) throw new Error('No inline script found in index.html')
  const source = match[1]

  // The two things the contract is about: which key, read as what.
  expect(source).toContain(`localStorage.getItem('${THEME_STORAGE_KEY}')`)
  expect(source).toContain("document.documentElement.classList.toggle('dark', dark)")
  expect(source).toContain('colorScheme')

  // Reset, then execute the real script body against this jsdom document.
  document.documentElement.classList.remove('dark')
  document.documentElement.style.colorScheme = ''
  new Function('matchMedia', 'localStorage', 'document', source)(
    window.matchMedia,
    window.localStorage,
    document,
  )
  return {
    dark: document.documentElement.classList.contains('dark'),
    colorScheme: document.documentElement.style.colorScheme,
  }
}

function resetStore(): void {
  useThemeStore.setState({
    pref: readStoredPref(),
    resolved: resolveTheme(
      readStoredPref(),
      window.matchMedia('(prefers-color-scheme: dark)').matches,
    ),
  })
}

describe('the storage-key contract with the inline no-flash script', () => {
  beforeEach(() => resetStore())

  it.each<ThemePref>(['light', 'dark', 'system'])(
    'writes %s as a plain string the inline script can read back',
    (pref) => {
      useThemeStore.getState().setPref(pref)

      // A plain string, not Zustand `persist`'s {state, version} envelope.
      expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe(pref)
      expect(() => JSON.parse(localStorage.getItem(THEME_STORAGE_KEY) ?? '')).toThrow()

      const painted = runInlineScriptLogic()
      expect(painted.dark).toBe(useThemeStore.getState().resolved === 'dark')
    },
  )

  it('agrees with the inline script when the OS prefers dark and pref is system', () => {
    setPrefersDark(true)
    useThemeStore.getState().setPref('system')

    expect(useThemeStore.getState().resolved).toBe('dark')
    expect(runInlineScriptLogic().dark).toBe(true)
  })

  it('defaults to system when nothing is stored, exactly as the inline script does', () => {
    localStorage.clear()
    expect(readStoredPref()).toBe('system')
    expect(runInlineScriptLogic().dark).toBe(false)
  })
})

describe('theme resolution matrix', () => {
  const cases: [ThemePref, boolean, ResolvedTheme][] = [
    ['light', false, 'light'],
    ['light', true, 'light'],
    ['dark', false, 'dark'],
    ['dark', true, 'dark'],
    ['system', false, 'light'],
    ['system', true, 'dark'],
  ]

  it.each(cases)('pref=%s prefersDark=%s resolves to %s', (pref, prefersDark, expected) => {
    expect(resolveTheme(pref, prefersDark)).toBe(expected)
  })

  it.each(cases)(
    'pref=%s prefersDark=%s paints the class and color-scheme',
    (pref, prefersDark, expected) => {
      setPrefersDark(prefersDark)
      resetStore()
      useThemeStore.getState().setPref(pref)

      expect(document.documentElement.classList.contains('dark')).toBe(expected === 'dark')
      expect(document.documentElement.style.colorScheme).toBe(expected)
    },
  )
})

describe('reacting to the system', () => {
  beforeEach(() => resetStore())

  it('re-resolves live when pref is system', () => {
    useThemeStore.getState().setPref('system')
    const stop = startThemeSync()

    setPrefersDark(true)
    expect(useThemeStore.getState().resolved).toBe('dark')
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    setPrefersDark(false)
    expect(useThemeStore.getState().resolved).toBe('light')
    stop()
  })

  it('ignores the system when pref is explicit', () => {
    useThemeStore.getState().setPref('light')
    const stop = startThemeSync()

    setPrefersDark(true)

    expect(useThemeStore.getState().resolved).toBe('light')
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    stop()
  })

  it('stops listening when the subscription is torn down', () => {
    useThemeStore.getState().setPref('system')
    const stop = startThemeSync()
    stop()

    setPrefersDark(true)
    expect(useThemeStore.getState().resolved).toBe('light')
  })
})

describe('persistence', () => {
  beforeEach(() => resetStore())

  it('survives a remount', () => {
    useThemeStore.getState().setPref('dark')
    resetStore()
    expect(useThemeStore.getState().pref).toBe('dark')
    expect(useThemeStore.getState().resolved).toBe('dark')
  })

  it('falls back to light rather than crashing when localStorage throws', () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('private mode')
    })
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('private mode')
    })

    expect(readStoredPref()).toBe('system')
    expect(() => applyTheme('light')).not.toThrow()
    expect(() => useThemeStore.getState().setPref('dark')).not.toThrow()

    getItem.mockRestore()
    setItem.mockRestore()
  })

  it('treats an unrecognised stored value as system', () => {
    localStorage.setItem(THEME_STORAGE_KEY, '{"state":{"pref":"dark"},"version":0}')
    expect(readStoredPref()).toBe('system')
  })
})
