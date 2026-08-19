/**
 * Golden flows #1, #2, and #7 — the coach acts on the board.
 *
 * docs/08-testing.md:
 *
 * > 1. **Create project → Socratic intake → first task list exists.**
 * > 2. **Big task gets broken up** — user asks for a 4-hour task; the coach adds subtasks
 * >    for it, one at a time; the parent card shows subtask count and summed duration.
 * > 7. **Preference adaptation** — set project task duration to 2 h; new agent-created
 * >    tasks respect it while another project still uses the 45-minute global default.
 *
 * The model is the deterministic stub (`MODEL_BACKEND=stub`), which plans from what the
 * learner says and from the budget it reads out of the rendered instruction
 * (`apps/api/src/coach/integrations/stub_model.py`). That parse is why flow #7 is
 * evidence rather than staging: nothing in the browser or the stub knows which project it
 * is in, so subtasks that follow a project's override can only have come from the prompt
 * the server assembled.
 *
 * **The board is asserted, not the chat.** These flows are about what the coach *did*,
 * and the board is where that lives — a transcript that says a task was added proves
 * nothing about `tasks/{id}`. Waiting on the board also exercises the `board_update`
 * push: the card appears without a reload and without the test refetching anything.
 */

import type { Page } from '@playwright/test';

import { expect, test } from './fixtures';

async function createProject(page: Page, title: string) {
  await page.goto('/');
  await page.getByLabel('New project').fill(title);
  await page.getByRole('button', { name: 'Create' }).click();
  await page.getByRole('link', { name: title }).click();
  await expect(page.getByRole('heading', { name: title })).toBeVisible();
}

async function say(page: Page, message: string) {
  await page.getByLabel('Message your coach').fill(message);
  await page.getByRole('button', { name: 'Send' }).click();
}

function cards(page: Page) {
  return page.getByTestId('task-card');
}

/** `formatMinutes`, as the parent card renders it. */
function duration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} m`;
}

test('flow #1: intake asks before it proposes, then the first tasks appear', async ({
  signedIn: page,
}) => {
  await createProject(page, 'Learn Rust');

  // The conversation is on the board itself: a project made from the list lands here, and
  // this is where the intake session (`taskId: null`) is rendered.
  await expect(page.getByTestId('session-hint')).toBeVisible();
  await expect(cards(page)).toHaveCount(0);

  // A message with no duration in it is a question, not a plan. The board stays empty,
  // which is the "do not produce a task list from a one-line prompt" behaviour.
  await say(page, 'I want to get good at Rust');
  await expect(page.getByTestId('transcript').locator('[data-role="model"]')).toHaveCount(1);
  await expect(cards(page)).toHaveCount(0);

  await say(page, 'I can give it 40 minutes a session');

  // No reload: the card arrives because the tool announced its write over the socket.
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page).first()).toContainText('40 min');
  await expect(cards(page).first()).toContainText('From your coach');
});

test('flow #2: a four-hour ask becomes subtasks that fit', async ({ signedIn: page }) => {
  await createProject(page, 'Build a compiler');

  await say(page, 'The parser is about 4 hours of work');
  await expect(cards(page)).toHaveCount(1);

  // 45 minutes is the global default, so the task is capped at three times that and
  // broken into three 45-minute pieces — three separate `add_subtask` calls in one turn,
  // where this used to be a single `split_task`. The card is the requirement: "number of
  // sub-tasks and total estimated duration" (docs/06-frontend.md#task-board).
  const parent = cards(page).first();
  await expect(parent).toContainText('3 subtasks');
  await expect(parent).toContainText(duration(135));

  // And the breakdown is real on the server, not a rendering of the chat.
  await page.reload();
  await expect(cards(page).first()).toContainText('3 subtasks');
});

test('flow #7: a project override changes how work is sized', async ({ signedIn: page }) => {
  await createProject(page, 'Two-hour sessions');
  await page.getByRole('link', { name: 'Project settings' }).click();
  // The field saves on blur, and `minutes-explainer` is what says the write landed —
  // waiting on it rather than on a fixed delay is what keeps this from racing the PATCH.
  await page.getByLabel('Default task length (minutes)').fill('120');
  await page.getByLabel('Default task length (minutes)').blur();
  await expect(page.getByTestId('minutes-explainer')).toContainText('in effect: 2 h');

  await page.goBack();
  await say(page, 'This chunk is about 4 hours of work');
  await expect(cards(page)).toHaveCount(1);
  // 240 minutes fits inside 3 x 120, so nothing is clamped and it comes out as two halves.
  await expect(cards(page).first()).toContainText('2 subtasks');
  await expect(cards(page).first()).toContainText(duration(240));

  // The other project, same sentence, still on the 45-minute global default.
  await createProject(page, 'On the global default');
  await say(page, 'This chunk is about 4 hours of work');
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page).first()).toContainText('3 subtasks');
  await expect(cards(page).first()).toContainText(duration(135));
});

test('what the coach did stays in the conversation', async ({ signedIn: page }) => {
  // The regression this exists for: chips rendered while the turn streamed and vanished
  // on `turn_complete`, because they lived only in `useStreamStore` and the transcript
  // dropped every event that carried a tool call. Reopening the session showed a
  // conversation in which tasks had appeared by themselves.
  await createProject(page, 'A record of the work');

  await say(page, 'The parser is about 4 hours of work');
  await expect(cards(page)).toHaveCount(1);

  const chips = page.getByTestId('transcript').getByTestId('tool-chips');
  await expect(chips.locator('[data-tool="add_task"]')).toBeVisible();
  await expect(chips.locator('[data-tool="add_subtask"]').first()).toBeVisible();
  // The detail, which is the point of a chip carrying one: *which* task, not merely that
  // a task was added.
  await expect(chips.locator('[data-tool="add_task"]')).toContainText('The parser');

  // Still there once the turn has settled into the transcript — the live buffer is gone
  // by now, so anything visible is coming from the stored events.
  await expect(page.getByTestId('live-turn')).toHaveCount(0);
  await expect(chips.locator('[data-tool="add_task"]')).toBeVisible();

  // And after a full reload, which is the state a user comes back to tomorrow.
  await page.reload();
  await expect(chips.locator('[data-tool="add_task"]')).toBeVisible();
  await expect(chips.locator('[data-tool="add_subtask"]').first()).toBeVisible();
  await expect(chips.locator('[data-tool="add_task"]')).toContainText('The parser');
});

test('the board refreshes after a tool call, even on a tab that opened elsewhere', async ({
  signedIn: page,
}) => {
  // The reported bug, and the path that produces it. `getSocket` is a singleton and React
  // runs child effects before parent ones, so loading a task workspace *first* had the
  // page build the socket for its presence heartbeat before `useCoachSocket` could hand
  // over its invalidation callback. The callback was dropped for the lifetime of the tab,
  // and from then on the board never refreshed when the coach changed it — which is
  // invisible to a flow that starts on the board, as every other flow here does.
  await createProject(page, 'Opened from a workspace');
  await page.getByLabel('New task').fill('Somewhere to start');
  await page.getByRole('button', { name: 'Add task' }).click();
  await expect(cards(page)).toHaveCount(1);

  await page.getByTestId('open-workspace').first().click();
  await expect(page.getByTestId('transcript')).toBeVisible();
  // A reload here is what makes the workspace the first screen to mount, rather than one
  // navigated to after the board had already built the socket.
  await page.reload();
  await expect(page.getByTestId('transcript')).toBeVisible();

  await page.getByRole('link', { name: '← Back to the board' }).click();
  await expect(cards(page)).toHaveCount(1);

  await say(page, 'The parser is about 4 hours of work');

  // No reload, no navigation: the board has to move on its own.
  await expect(cards(page)).toHaveCount(2);
  await expect(cards(page).nth(1)).toContainText('3 subtasks');
});

test('discarding a task waits for the learner to say so', async ({ signedIn: page }) => {
  await createProject(page, 'Housekeeping');
  await page.getByLabel('New task').fill('Something obsolete');
  await page.getByRole('button', { name: 'Add task' }).click();
  await expect(cards(page)).toHaveCount(1);

  await page.getByTestId('open-workspace').first().click();
  await expect(page.getByTestId('transcript')).toBeVisible();

  await say(page, 'Please discard that task');

  // The gate is ADK's, not the model's: the turn ends here and the tool has not run.
  const prompt = page.getByTestId('confirmation-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('Discard this task?');

  await prompt.getByRole('button', { name: 'Discard it' }).click();
  await expect(prompt).toBeHidden();

  await page.getByRole('link', { name: '← Back to the board' }).click();
  // Hidden by the default `Hide discarded` filter, which is the visible consequence.
  await expect(cards(page)).toHaveCount(0);
});

test('the coach asks a question with controls, and the answer is in the record', async ({
  signedIn: page,
}) => {
  /*
    `ask_learner` end to end. The mechanism is ADK's confirmation handshake carrying a
    payload rather than a yes/no, so what is being checked is that a structured answer
    survives the whole round trip: dialog → `POST /turns` → tool → stored transcript.

    The chip is the second half and the reason this is a flow rather than a unit test.
    docs/06-frontend.md makes the transcript the record of what the coach did; a chip that
    said only "Asking you something" would leave the learner's own answer nowhere at all.
  */
  await createProject(page, 'Build a compiler');

  await say(page, 'ask me something');

  const prompt = page.getByTestId('question-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('Which should we do first?');
  // Single-select, because that is what the tool asked for.
  await expect(prompt.getByRole('radio')).toHaveCount(2);
  await expect(prompt.getByRole('checkbox')).toHaveCount(0);

  await prompt.getByLabel('The parser').check();
  await prompt.getByLabel('Anything else I should know?').fill('I have done lexing before');
  await prompt.getByRole('button', { name: 'Send' }).click();

  // The dialog goes once answered — the pending state is derived from stored events, so
  // this also proves the answer was actually persisted rather than only sent.
  await expect(prompt).toBeHidden();

  const chip = page.getByTestId('tool-chips').last();
  await expect(chip).toContainText('The parser');
  await expect(chip).toContainText('I have done lexing before');

  // And it survives a reload, because it is in the transcript rather than in a buffer.
  await page.reload();
  await expect(page.getByTestId('tool-chips').last()).toContainText('The parser');
});

test('a question can ask for several answers at once', async ({ signedIn: page }) => {
  /*
    The mode that existed and was never used. `allow_multiple` has been wired since
    `ask_learner` landed, and the coach asked single-choice every time — the docstring
    described the flag neutrally and a model with no steer takes the simpler branch.

    Worth an e2e rather than only a unit test because the two modes render *different
    controls*: radios and checkboxes have different keyboard semantics, and a component
    that quietly rendered radios for a multi-select would pass every assertion about the
    payload while making the second answer unselectable.
  */
  await createProject(page, 'Async Python');

  await say(page, 'ask me about several things');

  const prompt = page.getByTestId('question-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt.getByRole('checkbox')).toHaveCount(3);
  await expect(prompt.getByRole('radio')).toHaveCount(0);

  // By role, not by label: the design system's checkbox renders a visually-hidden native
  // input beside the styled control, and `getByLabel` resolves to the hidden one.
  await prompt.getByRole('checkbox', { name: 'Generators' }).click();
  await prompt.getByRole('checkbox', { name: 'Async iterators' }).click();
  await prompt.getByRole('button', { name: 'Send' }).click();

  const chip = page.getByTestId('tool-chips').last();
  await expect(chip).toContainText('Generators, Async iterators');
});
