/**
 * Chip labels and details.
 *
 * A chip used to say only what *kind* of thing happened — "Adding a task" — which tells
 * the learner an action occurred and not which one. The detail is what makes the
 * transcript a record they can audit, and for `ask_learner` it is the only place their own
 * answer appears at all.
 *
 * Two rules the tests below exist to hold:
 *
 * - **Arguments before results.** A call still running, or refused, has no result. A
 *   summariser that needed one would blank exactly the chips worth reading.
 * - **An unknown tool degrades to nothing**, never to a crash or to `[object Object]`.
 *   Tool arguments come from a model and are not schema-checked anywhere on this path.
 */

import { describe, expect, it } from 'vitest';

import { describeTool, labelForTool } from '@/lib/tool-labels';

describe('labelForTool', () => {
  it('reads a known tool as copy', () => {
    expect(labelForTool('youtube_find_by_duration')).toBe('Checking video lengths');
  });

  it('degrades an unknown tool to a readable version of its name', () => {
    expect(labelForTool('some_future_tool')).toBe('some future tool');
  });
});

describe('describeTool', () => {
  it('says which task, not merely that a task was added', () => {
    expect(describeTool('add_task', { title: 'Read the asyncio guide', estimated_minutes: 45 })).toBe(
      'Read the asyncio guide (45 min)',
    );
  });

  it('reports a subtask taking over its parent’s checklist', () => {
    // The consequence the learner most needs told about, and one they cannot see from the
    // subtask alone: their steps moved.
    expect(
      describeTool(
        'add_subtask',
        { title: 'The first half', estimated_minutes: 20 },
        { inheritedItems: 3 },
      ),
    ).toBe('The first half (20 min) · took over 3 steps');
    expect(
      describeTool('add_subtask', { title: 'The first half' }, { inheritedItems: 0 }),
    ).toBe('The first half');
  });

  it('works from arguments alone, before any result exists', () => {
    // The live-chip case: `tool_result` carries only `ok`, so a streaming chip has
    // arguments and nothing else.
    expect(describeTool('discard_task', { reason: 'covered by the next task' })).toBe(
      'covered by the next task',
    );
    expect(
      describeTool('add_subtask', { title: 'The parser', estimated_minutes: 45 }),
    ).toBe('The parser (45 min)');
  });

  it('records the learner’s answer to a question, not the question alone', () => {
    expect(
      describeTool(
        'ask_learner',
        { question: 'Which should come first?' },
        { answered: true, selected: ['The parser'], note: 'I have done lexing before' },
      ),
    ).toBe('Which should come first? — The parser (I have done lexing before)');
  });

  it('renders several selections and an absent note', () => {
    expect(
      describeTool(
        'ask_learner',
        { question: 'Which do you already know?' },
        { answered: true, selected: ['Generators', 'Context managers'], note: '' },
      ),
    ).toBe('Which do you already know? — Generators, Context managers');
  });

  it('renders a declined question as an answer, because it is one', () => {
    expect(
      describeTool(
        'ask_learner',
        { question: 'Which of these have you used?' },
        { answered: false, selected: [] },
      ),
    ).toBe('Which of these have you used? — none of these');
  });

  it('shows a completed item and says when it finished the task', () => {
    expect(
      describeTool(
        'complete_task_item',
        { note: 'you talked me through cancellation' },
        { taskCompleted: true },
      ),
    ).toBe('you talked me through cancellation — task finished');
    expect(
      describeTool('complete_task_item', { note: 'read it' }, { taskCompleted: false }),
    ).toBe('read it');
  });

  it('reports how many videos actually fit the budget', () => {
    expect(
      describeTool(
        'youtube_find_by_duration',
        { query: 'structured concurrency' },
        { ok: true, videos: [{}, {}] },
      ),
    ).toBe('structured concurrency — 2 that fit');
  });

  it('reports a report against its budget', () => {
    expect(
      describeTool('post_research_report', {}, { requiredMinutes: 38, budgetMinutes: 45 }),
    ).toBe('38 of 45 min of material');
  });

  it('returns nothing for a tool with no summariser', () => {
    expect(describeTool('some_future_tool', { anything: 1 })).toBe('');
  });

  it('survives arguments of the wrong shape', () => {
    // These come from a model. Nothing on this path validates them, and a transcript must
    // not fail to render because a tool call had a number where a string was expected.
    expect(describeTool('add_task', { title: 42, estimated_minutes: 'soon' })).toBe('');
    expect(describeTool('add_subtask', { title: 42 })).toBe('');
    expect(describeTool('ask_learner', { question: null }, { selected: 'one' })).toBe('');
  });
});
