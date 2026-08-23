/**
 * Golden flow #5 — the manual research trigger.
 *
 * docs/08-testing.md:
 *
 * > 5. **Manual research trigger** — user creates a task, clicks "Research this task now",
 * >    lands on that run's research view and sees progress chips there, then follows
 * >    "back to task" once it completes and sees the checklist populated and the research
 * >    card showing the finished job. The task was `draft` before the run and is
 * >    `not_started` after it, which is the visible half of invariant 1; then ticking every
 * >    checklist item completes the task without the learner touching a state control,
 * >    which is the visible half of invariant 6.
 *
 * **Since M8** the research turn runs in a session of its own, watched from its own
 * `/projects/:projectId/research/:runId` view rather than inline in the task workspace —
 * see docs/06-frontend.md#research-view-projectsprojectidresearchrunid. `researchTask`
 * below is the round trip every test in this file now makes: click the button, watch the
 * run finish on its own screen, and follow "Open task" back.
 *
 * The model is the deterministic stub, which recognises `research_agent` by its tool set
 * and sizes its report from the **research** budget line the prompt carries — the task's
 * own estimate, not the project default. So a 30-minute task getting a 30-minute checklist
 * is evidence that the estimate reached the model, on the same footing as flow #7's
 * duration override (`apps/api/src/coach/integrations/stub_model.py`).
 *
 * **The two state changes are the point of the flow, not decoration.** They are the halves
 * of M4 that no unit test sees end to end: a badge that moves because the server derived a
 * state, and a task that completes because two checkboxes were ticked and nothing else.
 */

import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

/**
 * Click the button that starts research on the current task workspace, wait for the run
 * to finish on its own research view, and follow "Open task" back — the whole M8 round
 * trip, since the checklist and the report no longer render on the same screen.
 *
 * `buttonName` distinguishes "Research this task now" from "Research again", the same
 * pair the pre-M8 version of this flow drove directly.
 */
async function researchTask(
  page: Page,
  buttonName = 'Research this task now',
): Promise<string> {
  await page.getByRole('button', { name: buttonName }).click();
  await page.waitForURL(/\/research\//);
  const researchUrl = page.url();

  // The chips come from the research turn's `tool_call` frames, rendered by the same
  // `Transcript` the task's own chat uses — a research run is an ordinary turn, which is
  // what lets it reuse the streaming path unchanged.
  await expect(page.getByTestId('tool-chips').first()).toBeVisible();
  const report = page.getByTestId('research-report');
  await expect(report).toBeVisible();

  // Optional material has no completion checkbox — a distinct landmark from the required
  // list, which is a product requirement checked wherever the report renders.
  const optional = page.getByTestId('report-optional');
  await expect(optional).toBeVisible();
  await expect(optional.getByRole('checkbox')).toHaveCount(0);

  await page.getByRole('link', { name: 'Open task' }).click();
  return researchUrl;
}

/*
  A research run is two model round trips — the `post_research_report` call, then the
  closing prose — where every other flow in the suite is one. The re-run test does two of
  them back to back, which does not fit Playwright's 30 s default once the suite is running
  several workers on one machine.

  Raised here rather than by padding the individual `toBeVisible` timeouts, because those
  were the symptom: an assertion given 30 s inside a test given 30 s can never actually
  wait 30 s, so it fails at whatever is left over and reads as a product bug. The per-
  assertion waits below are back at the default.
*/
test.describe.configure({ timeout: 90_000 });

async function send(page: Page, message: string) {
  await page.getByLabel('Message your coach').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
}

async function openWorkspace(
  page: Page,
  projectTitle: string,
  taskTitle: string,
  minutes = 30,
) {
  await page.goto('/');
  await page.getByLabel('New project').fill(projectTitle);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: projectTitle }).click();

  await page.getByLabel('New task').fill(taskTitle);
  await page.getByLabel('Minutes').fill(String(minutes));
  await page.getByRole('button', { name: 'Add task' }).click();
  await page.getByTestId('open-workspace').filter({ hasText: taskTitle }).click();

  await expect(page.getByRole('heading', { name: taskTitle })).toBeVisible();
}

test('flow #5: research fills the checklist, and finishing it completes the task', async ({
  signedIn: page,
}) => {
  await openWorkspace(page, 'Async Python', 'Structured concurrency', 30);

  // A brand-new task has no plan, and the badge says so rather than calling it "not
  // started" — which would be a claim about the learner rather than about the task.
  await expect(page.getByText('No plan yet')).toBeVisible();
  await expect(page.getByTestId('checklist')).toBeHidden();

  await researchTask(page);

  const checklist = page.getByTestId('checklist');
  await expect(checklist).toBeVisible();

  // Invariant 1, from the outside: the task acquired a plan, so it left `draft`.
  await expect(page.getByText('Not started')).toBeVisible();
  await expect(page.getByText('No plan yet')).toBeHidden();

  // The report's required list became the checklist; its optional list stayed on the
  // report, with no checkbox. Distinct landmarks, which is the product requirement —
  // checked on the research view we just came from, where the report now lives.
  await expect(checklist.getByRole('checkbox')).toHaveCount(2);

  // Since M8 the task's own screen shows a compact card for the run rather than the full
  // report, linking back into the research view.
  await expect(page.getByTestId('research-card')).toBeVisible();

  // The budget meter sums the checklist against the *task's* 30 minutes, not the project's
  // 45-minute default — the number the stub read out of the rendered instruction.
  await expect(page.getByTestId('checklist-budget')).toContainText('30 min of 30 min');

  // One guided, one unguided. The guided one carries the marker and — asserted in
  // `Checklist.test.tsx` and again here because it is the rule most likely to be undone by
  // a well-meaning UI change — never its details.
  await expect(checklist.getByText('with your coach')).toHaveCount(1);
  await expect(
    checklist.getByText('Ask them to explain it back before showing them the answer.'),
  ).toBeHidden();

  const boxes = checklist.getByRole('checkbox');
  await boxes.first().click();

  // One of two done. Still open — the task completes when the checklist does, not when it
  // starts to.
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');
  await expect(page.getByText('Completed')).toBeHidden();

  await boxes.last().click();

  // Invariant 6, from the outside. Nothing on this page was clicked except two checkboxes:
  // no state control, no "Complete" action. That is the behaviour M4 added, and it is also
  // what keeps Q1 honest — the last thing that happened was still the learner's click.
  await expect(page.getByText('Completed')).toBeVisible();
});

test('flow #5: a second research run replaces the checklist without losing finished work', async ({
  signedIn: page,
}) => {
  await openWorkspace(page, 'Rust ownership', 'Borrow checker', 30);

  const firstRunUrl = await researchTask(page);
  const checklist = page.getByTestId('checklist');
  await expect(checklist).toBeVisible();

  await checklist.getByRole('checkbox').first().click();
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');

  // "Research again" rather than a second primary button: once materials exist the action
  // is a re-run, and the copy says so (docs/06-frontend.md).
  const secondRunUrl = await researchTask(page, 'Research again');

  // Since M8 each run gets its own session and its own research view — a re-run is a
  // *different* run, not a revisit of the first one's screen.
  expect(secondRunUrl).not.toBe(firstRunUrl);

  // The stub is deterministic, so the second run produces the same two items — which is
  // exactly the case the identity match exists for. The tick survives, because a re-run
  // that keeps a reading the learner has already done must not ask for it again
  // (docs/02-data-model.md#task-items).
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');
  await expect(checklist.getByRole('checkbox')).toHaveCount(2);

  // The first run did not vanish — it is reachable from "View previous research" beside
  // the card for the one that replaced it.
  await page.getByTestId('toggle-previous-research').click();
  const previous = page.getByTestId('previous-research').getByTestId('research-card');
  await expect(previous).toHaveCount(1);
  await expect(previous).toHaveAttribute('href', new URL(firstRunUrl).pathname);
});

test('flow #5: the board shows a leaf task’s checklist progress', async ({
  signedIn: page,
}) => {
  await openWorkspace(page, 'Board progress', 'A researched task', 30);

  await researchTask(page);
  await expect(page.getByTestId('checklist')).toBeVisible();
  await page.getByTestId('checklist').getByRole('checkbox').first().click();
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');

  await page.getByRole('link', { name: '← Back to the board' }).click();

  // The same slot a parent card uses for its subtask rollup — they are the same idea, and
  // a task never has both.
  const card = page.getByTestId('task-card').first();
  await expect(card.getByTestId('item-progress')).toContainText('1 of 2 done');
  await expect(card.getByTestId('materials-ready')).toBeVisible();
});

test('a subtask’s checklist is visible and tickable inside the parent', async ({
  signedIn: page,
}) => {
  /*
    A subtask has no route of its own (docs/06-frontend.md), which has to mean *reachable
    from the parent* rather than unreachable. It holds items exactly as a leaf does — the
    first subtask inherits the parent's when the parent becomes composite — so without this
    the coach could plan work the learner had no way to see, let alone tick off.

    The inheritance is the interesting half: the checklist is built by a research run on the
    parent, and then a subtask takes it over.
  */
  await openWorkspace(page, 'Compilers', 'Write the parser', 30);

  await researchTask(page);
  await expect(page.getByTestId('checklist')).toBeVisible();
  await page.getByTestId('checklist').getByRole('checkbox').first().click();
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');

  // Making it composite moves those steps — ticks and all — onto the new subtask.
  await page.getByLabel('New subtask').fill('Tokenizing');
  await page.getByLabel('Minutes').fill('20');
  await page.getByRole('button', { name: 'Add subtask' }).click();

  const card = page.getByTestId('subtask-card').filter({ hasText: 'Tokenizing' });
  await expect(card).toBeVisible();
  await expect(card.getByTestId('checklist')).toContainText('Steps for this subtask');
  await expect(card.getByTestId('checklist-budget')).toContainText('1 of 2 done');

  // And the parent's own checklist is gone, because a task's plan is its items or its
  // subtasks and never both.
  await expect(page.getByTestId('checklist')).toHaveCount(1);

  // Tickable in place, against the *subtask* — the write goes to a different task from the
  // one this screen is keyed on, which is the whole reason it needs its own mutation.
  await card.getByRole('checkbox').last().click();
  await expect(card.getByTestId('checklist-budget')).toContainText('2 of 2 done');
  await expect(card.getByTestId('subtask-state')).toContainText('Completed');
});

test('the completion gate can be silenced from the dialog it interrupts', async ({
  signedIn: page,
}) => {
  /*
    Three things in one round trip: the step is ticked, the project preference is written,
    and the next completion does not ask. Worth an e2e because no unit test spans them —
    the flag rides in the confirmation's answer payload, is read by the tool, and takes
    effect through a *dynamic* `require_confirmation` that ADK evaluates per call.

    The second completion is the assertion that matters. A version of this that stopped at
    "the preference was written" would pass against a gate that only re-read the setting at
    process start.
  */
  await openWorkspace(page, 'Drills', 'Quick practice', 30);
  await researchTask(page);
  await expect(page.getByTestId('checklist')).toBeVisible();

  await send(page, 'mark the first step done');
  const prompt = page.getByTestId('confirmation-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('Mark this step done?');

  await prompt
    .getByRole('button', { name: 'Mark it done and stop asking in this project' })
    .click();
  await expect(page.getByTestId('checklist-budget')).toContainText('1 of 2 done');

  // Asked again, the coach ticks without interrupting.
  await send(page, 'mark the first step done');
  await expect(page.getByTestId('checklist-budget')).toContainText('2 of 2 done');
  await expect(page.getByTestId('confirmation-prompt')).toBeHidden();

  // And the setting is visible where it can be turned back on.
  await page.getByRole('link', { name: '← Back to the board' }).click();
  await page.getByRole('link', { name: 'Project settings' }).click();
  // By role: the design system's switch renders a styled button beside a hidden input, and
  // `getByLabel` resolves to the input rather than to the thing carrying `aria-checked`.
  await expect(
    page.getByRole('switch', { name: 'Ask before your coach ticks off a step' }),
  ).not.toBeChecked();
});

test('M8: research with no task, from the board, produces a taskless report and a board card', async ({
  signedIn: page,
}) => {
  /*
    The new capability this milestone adds: a question about the project as a whole,
    kicked off beside the board rather than from inside a task. Nothing here is promoted
    onto any task's checklist, because there is no task.
  */
  await page.goto('/');
  await page.getByLabel('New project').fill('Choosing a stack');
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: 'Choosing a stack' }).click();

  await page.getByRole('button', { name: 'Research something for this project' }).click();
  await page
    .getByLabel('What should your coach research?')
    .fill('What is a good first project to build?');
  await page.getByRole('button', { name: 'Research this' }).click();

  await page.waitForURL(/\/research\//);
  await expect(page.getByRole('heading', { name: 'Research for this project' })).toBeVisible();
  // No task to open — the affordance every task-scoped run's header carries is absent.
  await expect(page.getByRole('link', { name: 'Open task' })).toHaveCount(0);

  await expect(page.getByTestId('tool-chips').first()).toBeVisible();
  const report = page.getByTestId('research-report');
  await expect(report).toBeVisible();
  // The required list is a plan for the question, not a checklist — rendered here because
  // there is no task for it to live on instead.
  await expect(report.getByTestId('report-required')).toContainText(
    'What answers the question',
  );

  await page.getByRole('link', { name: '← Back to the board' }).click();
  await expect(page.getByTestId('research-card')).toBeVisible();
  await expect(page.getByTestId('research-card')).toContainText('Project');
});
