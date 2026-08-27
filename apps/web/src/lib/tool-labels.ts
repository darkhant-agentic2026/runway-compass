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
  add_subtask: 'Adding a subtask',
  set_task_state: 'Updating the board',
  set_next_up: 'Choosing what is next',
  reorder_task: 'Reordering the board',
  list_tasks: 'Looking at your board',
  add_task_items: 'Adding steps to this task',
  complete_task_item: 'Marking a step done',
  update_task: 'Updating a task',
  update_project_prefs: 'Updating your preferences',
  update_project_plan: 'Proposing project plan',
  discard_task: 'Discarding a task',
  search_agent: 'Searching the web',
  load_memory: 'Remembering earlier sessions',
  update_learner_profile: 'Updating learner profile',
  remember: 'Remembering insight',
  write_roadmap_brief: 'Writing the roadmap brief',
  read_roadmap_brief: 'Checking the roadmap brief',
  propose_roadmap_brief: 'Proposing the roadmap',
  view_study_plan: 'Looking at the study plan',
  revise_study_plan: 'Revising the study plan',
  materialize_study_plan: 'Creating tasks from the study plan',
};

export function labelForTool(name: string): string {
  return LABELS[name] ?? name.replaceAll('_', ' ');
}

/**
 * A one-line summary of what a tool call actually did.
 *
 * The label alone says an action happened; this says *which*. "Adding a task" and "Adding
 * a task · Read the asyncio guide (45 min)" are the difference between a record the
 * learner can audit and a record that merely proves the coach was busy — and for
 * `ask_learner` the label alone would hide the learner's own answer behind "Asking you
 * something".
 *
 * **Read from arguments first, result second.** A call that is still running, or that was
 * refused, has no result — so a summariser that needed one would leave exactly the chips
 * worth reading blank. `ask_learner` is the deliberate exception: the interesting half of
 * that call is the *answer*, which only exists in the result.
 *
 * Unknown tools return `''` and the chip renders its label alone, which is the same
 * graceful degradation `labelForTool` gives an unknown name: adding a tool never requires
 * a change here first.
 */
export function describeTool(
  name: string,
  args: Record<string, unknown>,
  result?: Record<string, unknown>,
): string {
  const summarise = DETAILS[name];
  if (!summarise) return '';
  try {
    return summarise(args, result ?? {}).trim();
  } catch {
    // A summariser is decoration on a record of what happened. Nothing it can hit is worth
    // failing a transcript render for.
    return '';
  }
}

type Summariser = (args: Record<string, unknown>, result: Record<string, unknown>) => string;

/** A string argument, or `''`. Tool args arrive from a model and are not schema-checked. */
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function minutes(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? ` (${value} min)` : '';
}

function list(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : [];
}

const DETAILS: Record<string, Summariser> = {
  add_task: (args) =>
    text(args.title) + minutes(args.estimated_minutes ?? args.estimatedMinutes),
  add_subtask: (args, result) => {
    const inherited = result.inheritedItems;
    const moved =
      typeof inherited === 'number' && inherited > 0
        ? ` · took over ${inherited} ${inherited === 1 ? 'step' : 'steps'}`
        : '';
    return text(args.title) + minutes(args.estimated_minutes ?? args.estimatedMinutes) + moved;
  },
  update_task: (args) =>
    [text(args.title), minutes(args.estimated_minutes ?? args.estimatedMinutes).trim()]
      .filter(Boolean)
      .join(' '),
  set_task_state: (args) => text(args.state).replaceAll('_', ' '),
  discard_task: (args) => text(args.reason),
  add_task_items: (args) => {
    const items = Array.isArray(args.items) ? args.items : [];
    const first = items[0] as Record<string, unknown> | undefined;
    const lead = text(first?.shortDescription ?? first?.short_description);
    if (items.length <= 1) return lead;
    return `${lead} +${items.length - 1} more`;
  },
  update_task_item: (args) => text(args.short_description ?? args.shortDescription),
  reorder_task_item: () => 'moved a step',
  delete_task_item: (args) => text(args.reason),
  complete_task_item: (args, result) =>
    result.taskCompleted === true ? `${text(args.note)} — task finished` : text(args.note),
  update_project_prefs: (args) =>
    Object.entries(args)
      .filter(([, value]) => value !== null && value !== undefined)
      .map(([key, value]) => `${key.replaceAll('_', ' ')}: ${String(value)}`)
      .join(', '),
  update_project_plan: (args, result) => {
    if (result.accepted === false) return 'Plan declined — continuing to refine';
    const summary = text(args.summary);
    const tasks = Array.isArray(args.tasks) ? args.tasks : [];
    if (summary) return summary;
    if (tasks.length > 0)
      return `${tasks.length} ${tasks.length === 1 ? 'task' : 'tasks'} planned`;
    return 'Project plan updated';
  },
  update_learner_profile: (args) => {
    const parts = [];
    if (args.thinking_style || args.thinkingStyle) parts.push('thinking style');
    if (Array.isArray(args.strengths) && args.strengths.length) parts.push('strengths');
    if (Array.isArray(args.gaps) && args.gaps.length) parts.push('gaps');
    if (Array.isArray(args.skills) && args.skills.length) parts.push('skills');
    return parts.length ? `Updated ${parts.join(', ')}` : 'Profile updated';
  },
  remember: (args) => text(args.text),
  fetch_url: (args) => text(args.url),
  youtube_find_by_duration: (args, result) => {
    const found = Array.isArray(result.videos) ? result.videos.length : null;
    const query = text(args.query);
    if (found === null) return query;
    return `${query} — ${found} that fit`;
  },
  post_research_report: (_args, result) => {
    const required = result.requiredMinutes;
    const budget = result.budgetMinutes;
    if (typeof required !== 'number' || typeof budget !== 'number') return '';
    return `${required} of ${budget} min of material`;
  },
  /**
   * The learner's own answer, which is the whole point of the chip.
   *
   * From the *result*, deliberately: the arguments are the question, and a transcript that
   * recorded the question without the answer would be a record of the coach talking. A
   * declined question reads as "none of these" rather than as a blank, because declining
   * is an answer.
   */
  ask_learner: (args, result) => {
    const question = text(args.question);
    if (result.answered === false) return `${question} — none of these`;
    const chosen = list(result.selected);
    if (chosen.length === 0) return question;
    const note = text(result.note);
    return `${question} — ${chosen.join(', ')}${note ? ` (${note})` : ''}`;
  },
  write_roadmap_brief: (args) => text(args.subject),
  propose_roadmap_brief: (args, result) => {
    if (result.scheduled === false) return 'Roadmap declined — continuing to refine';
    return text(args.subject);
  },
  revise_study_plan: (_args, result) => {
    const included = result.includedCount;
    const total = result.taskCount;
    if (typeof included !== 'number' || typeof total !== 'number') return '';
    return `${included} of ${total} tasks kept`;
  },
  materialize_study_plan: (_args, result) => {
    const created = result.createdCount;
    if (typeof created !== 'number') return '';
    return `${created} ${created === 1 ? 'task' : 'tasks'} created`;
  },
};
