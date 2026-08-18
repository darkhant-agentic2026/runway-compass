/**
 * Theme: light, dark, system.
 *
 * docs/06-frontend.md#theme-light-dark-system. Two distinct values, and conflating them
 * is the usual source of bugs:
 *
 * - **`pref`** — what the user chose: `light | dark | system`. Persisted.
 * - **`resolved`** — what is actually painted: `light | dark`. Derived; never stored as
 *   the user's choice.
 *
 * `system` is a real, persistent state, not the absence of a choice — which is why the
 * control is a three-way segmented control and never a binary toggle.
 *
 * ## The storage-key contract
 *
 * The inline script in `index.html` reads `localStorage['coach.theme']` **as a plain
 * string** before React mounts. This store therefore writes that key directly rather than
 * going through Zustand's `persist` middleware, whose `{state, version}` envelope the
 * inline reader would choke on. The coupling is deliberate and fragile; `theme.test.ts`
 * pins it by running the inline script's own logic against what this store wrote.
 *
 * Theme lives in `localStorage` rather than in `globalPrefs` because it has to be
 * readable before auth resolves — the login screen needs a theme too.
 */

import { create } from 'zustand';

export type ThemePref = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

/** Shared with the inline script in `index.html`. Changing this breaks the no-flash path. */
export const THEME_STORAGE_KEY = 'coach.theme';

const DARK_QUERY = '(prefers-color-scheme: dark)';

function isThemePref(value: unknown): value is ThemePref {
  return value === 'light' || value === 'dark' || value === 'system';
}

/** Read the persisted preference. Falls back to `system` on anything unexpected. */
export function readStoredPref(): ThemePref {
  try {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePref(raw) ? raw : 'system';
  } catch {
    // Private mode: fall through rather than crashing the app.
    return 'system';
  }
}

function writeStoredPref(pref: ThemePref): void {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, pref);
  } catch {
    /* private mode: the theme simply will not persist */
  }
}

function systemPrefersDark(): boolean {
  try {
    return window.matchMedia(DARK_QUERY).matches;
  } catch {
    return false;
  }
}

export function resolveTheme(pref: ThemePref, prefersDark: boolean): ResolvedTheme {
  if (pref === 'system') return prefersDark ? 'dark' : 'light';
  return pref;
}

/**
 * Paint the resolved theme onto the document.
 *
 * `color-scheme` is set alongside the class: without it, native scrollbars, form
 * controls, and the pre-paint canvas stay light while everything else goes dark.
 * `<meta name="theme-color">` is updated so mobile browser chrome matches.
 */
export function applyTheme(resolved: ResolvedTheme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.toggle('dark', resolved === 'dark');
  root.style.colorScheme = resolved;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', resolved === 'dark' ? '#0a0a0a' : '#ffffff');
}

interface ThemeState {
  pref: ThemePref;
  resolved: ResolvedTheme;
  setPref: (pref: ThemePref) => void;
  /** Called by the `matchMedia` listener; a no-op unless `pref` is `system`. */
  syncWithSystem: (prefersDark: boolean) => void;
}

export const useThemeStore = create<ThemeState>((set, get) => {
  const pref = readStoredPref();
  return {
    pref,
    resolved: resolveTheme(pref, systemPrefersDark()),

    setPref(next) {
      const resolved = resolveTheme(next, systemPrefersDark());
      writeStoredPref(next);
      applyTheme(resolved);
      set({ pref: next, resolved });
    },

    syncWithSystem(prefersDark) {
      // Attached always, but inert unless the user chose `system` — simpler than
      // attaching and detaching the listener on every preference change.
      if (get().pref !== 'system') return;
      const resolved = resolveTheme('system', prefersDark);
      applyTheme(resolved);
      set({ resolved });
    },
  };
});

/**
 * Attach the OS listener. Called once from the app root.
 *
 * Returns an unsubscribe so tests and hot reloads do not stack listeners.
 */
export function startThemeSync(): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return () => {};
  }
  const query = window.matchMedia(DARK_QUERY);
  const onChange = (event: MediaQueryListEvent) => {
    useThemeStore.getState().syncWithSystem(event.matches);
  };
  query.addEventListener('change', onChange);
  // The inline script has already painted; re-apply so a remount cannot drift from it.
  applyTheme(useThemeStore.getState().resolved);
  return () => query.removeEventListener('change', onChange);
}
