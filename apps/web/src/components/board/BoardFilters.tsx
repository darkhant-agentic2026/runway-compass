/**
 * Board filters.
 *
 * docs/06-frontend.md: "Hide completed (default **on**), Hide discarded (default on),
 * Hide postponed (default off). Persisted per project in `useBoardUiStore`."
 */

import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import type { BoardFilters as Filters } from '@/stores/boardUi'

const OPTIONS: { key: keyof Filters; label: string }[] = [
  { key: 'hideCompleted', label: 'Hide completed' },
  { key: 'hideDiscarded', label: 'Hide discarded' },
  { key: 'hidePostponed', label: 'Hide postponed' },
]

export function BoardFilters({
  filters,
  onToggle,
}: {
  filters: Filters
  onToggle: (filter: keyof Filters) => void
}) {
  return (
    <fieldset className="flex flex-wrap items-center gap-4">
      <legend className="sr-only">Board filters</legend>
      {OPTIONS.map(({ key, label }) => (
        <div key={key} className="flex items-center gap-2">
          <Switch
            id={`filter-${key}`}
            checked={filters[key]}
            onCheckedChange={() => onToggle(key)}
          />
          <Label htmlFor={`filter-${key}`} className="text-sm font-normal">
            {label}
          </Label>
        </div>
      ))}
    </fieldset>
  )
}
