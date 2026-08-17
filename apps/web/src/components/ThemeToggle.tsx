/**
 * The Appearance control.
 *
 * docs/06-frontend.md#the-control: a three-way segmented control with `radiogroup`
 * semantics, arrow-key navigable. Never a binary toggle — `system` is a real, persistent
 * state, not the absence of a choice. When `System` is selected it says what that
 * currently means, so the resolved state is never a mystery.
 */

import { Monitor, Moon, Sun } from 'lucide-react'

import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { useThemeStore, type ThemePref } from '@/stores/theme'

const OPTIONS: { value: ThemePref; label: string; Icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', Icon: Sun },
  { value: 'dark', label: 'Dark', Icon: Moon },
  { value: 'system', label: 'System', Icon: Monitor },
]

export function ThemeToggle() {
  const pref = useThemeStore((state) => state.pref)
  const resolved = useThemeStore((state) => state.resolved)
  const setPref = useThemeStore((state) => state.setPref)

  return (
    <div className="space-y-2">
      {/*
        shadcn's current registry style builds on Base UI rather than Radix, so the
        group's value is an array and the item semantics are `aria-pressed` toggle
        buttons rather than Radix's `radiogroup`. Arrow-key navigation and the
        three-way shape that docs/06-frontend.md#the-control asks for are unaffected.
      */}
      <ToggleGroup
        value={[pref]}
        onValueChange={(value) => {
          // Base UI reports an empty array when the active item is pressed again; keep
          // the current choice rather than falling into an unset state, because
          // "no theme" is not one of the three options.
          const next = value[0]
          if (next) setPref(next as ThemePref)
        }}
        aria-label="Theme"
        variant="outline"
      >
        {OPTIONS.map(({ value, label, Icon }) => (
          <ToggleGroupItem key={value} value={value} aria-label={label}>
            <Icon className="size-4" aria-hidden="true" />
            <span>{label}</span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>

      <p className="text-muted-foreground text-sm" data-testid="theme-explainer">
        {pref === 'system'
          ? `System — currently ${resolved}`
          : `Always ${pref}`}
      </p>
    </div>
  )
}
