/**
 * Duration formatting.
 *
 * docs/06-frontend.md: "Duration formatting is one shared `formatMinutes()`
 * (`45 min`, `1 h 30 m`) used by cards, rollups, and the budget meter." One function so
 * a card and a rollup cannot disagree about what 90 minutes looks like.
 */

export function formatMinutes(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return '0 min'

  const whole = Math.round(minutes)
  const hours = Math.floor(whole / 60)
  const rest = whole % 60

  if (hours === 0) return `${rest} min`
  if (rest === 0) return `${hours} h`
  return `${hours} h ${rest} m`
}

/** "3 of 4 subtasks" and friends, so pluralisation lives in one place too. */
export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`
}
