/**
 * Friendly labels for tool-activity chips.
 *
 * docs/06-frontend.md#task-workspace: "tool activity as inline status chips ('Searching
 * the web…', 'Checking video lengths…')".
 *
 * These live on the client because they are copy, not protocol: the `tool_call` frame
 * carries the tool's real name, and a tool nobody has written a label for yet degrades to
 * a readable version of that name rather than to nothing. Adding a tool never requires a
 * change here first.
 *
 * Separate from `ToolChips.tsx` so that file exports only components, which is what keeps
 * Vite's fast refresh working for it.
 */

const LABELS: Record<string, string> = {
  google_search: 'Searching the web',
  fetch_url: 'Reading a page',
  youtube_find_by_duration: 'Checking video lengths',
  post_research_report: 'Writing up what it found',
  add_task: 'Adding a task',
  split_task: 'Splitting a task',
  set_task_state: 'Updating the board',
  set_next_up: 'Choosing what is next',
  reorder_task: 'Reordering the board',
  list_tasks: 'Looking at your board',
  load_memory: 'Remembering earlier sessions',
}

export function labelForTool(name: string): string {
  return LABELS[name] ?? name.replaceAll('_', ' ')
}
