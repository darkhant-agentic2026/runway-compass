/**
 * Timing for a newly-appeared board card's entrance
 * (docs/09-roadmap.md#task-board-and-task-view-polish): the card fades in, its title reveals
 * letter by letter, then its content — badges, duration, state, progress, item kinds — fades
 * in one piece at a time. `BoardPage` uses `titleRevealMs` to work out when the *next* new
 * card in the same batch should start — as soon as this one's title finishes.
 *
 * `AnimatedText` and `LETTER_INTERVAL_MS` are shared with two other spots that play the same
 * letter-by-letter reveal the first time a value *appears* while its page is already open,
 * never on that page's own first load (`useDescriptionReveal`): the board's own project
 * description (`BoardPage`, the coach naming the project mid-intake-conversation) and a
 * task's description on the workspace (`TaskWorkspacePage`, a plan-materialization write).
 *
 * `TITLE_LETTERS_PER_SECOND` is the one knob meant to be retuned by feel later; everything
 * else derives from it so the card, its title, and its chips stay in step.
 */

import { useEffect, useRef, useState } from 'react';

export const TITLE_LETTERS_PER_SECOND = 30;
export const LETTER_INTERVAL_MS = 1000 / TITLE_LETTERS_PER_SECOND;

/** Must match `.animate-task-letter-in`'s `animation-duration` in `index.css`. */
const LETTER_FADE_MS = 200;

/** Must match `.animate-task-chip-in`'s `animation-duration` in `index.css`. Also the
 * spacing between a card's content pieces revealing one after another, once its title has
 * finished — a shorter gap than the letter-by-letter title reads as one beat too fast. */
const CHIP_INTERVAL_MS = 90;

/** How long `text` takes to finish revealing, letter by letter. */
export function titleRevealMs(text: string): number {
  return [...text].length * LETTER_INTERVAL_MS + LETTER_FADE_MS;
}

/** A string's letters, each fading in on its own delay — the "typing" effect shared by a
 * new board card's title and a task's freshly-written description. Renders a bare fragment
 * of spans; the caller supplies the surrounding element (an `<a>`, a `<p>`). */
export function AnimatedText({ text, startDelayMs }: { text: string; startDelayMs: number }) {
  return (
    <>
      {[...text].map((char, index) => (
        <span
          key={index}
          className="animate-task-letter-in"
          style={{ animationDelay: `${startDelayMs + index * LETTER_INTERVAL_MS}ms` }}
        >
          {char}
        </span>
      ))}
    </>
  );
}

/**
 * Hands out one `animation-delay` slot per call, `CHIP_INTERVAL_MS` apart, starting at
 * `afterMs` — a card's content pieces (badges, duration, state, progress, item kinds) fade
 * in one at a time once its title has finished, rather than together. Call it once per piece
 * *that will actually render*, in the order it appears on the card, so a card with fewer
 * pieces doesn't leave gaps in the sequence.
 */
export interface ChipReveal {
  className: string;
  style: { animationDelay: string };
}

export function chipRevealSequence(afterMs: number): () => ChipReveal {
  let index = 0;
  return () => {
    const delay = afterMs + index * CHIP_INTERVAL_MS;
    index += 1;
    return { className: 'animate-task-chip-in', style: { animationDelay: `${delay}ms` } };
  };
}

/**
 * The entrance delay for each task in `tasks` that is not in `seenIds` — `undefined` means
 * "the board hasn't loaded before" (`BoardPage`'s ref starts `null`), so nothing is new and
 * the map comes back empty. A task already in `seenIds` gets no entry at all, which is how
 * the caller tells "new" from "already on screen" apart.
 */
export function computeRevealDelays(
  tasks: readonly { id: string; title: string }[],
  seenIds: ReadonlySet<string> | null,
): Map<string, number> {
  const delays = new Map<string, number>();
  if (!seenIds) return delays;
  let cumulative = 0;
  for (const task of tasks) {
    if (seenIds.has(task.id)) continue;
    delays.set(task.id, cumulative);
    cumulative += titleRevealMs(task.title);
  }
  return delays;
}

const EMPTY_DELAYS: Map<string, number> = new Map();

/**
 * `BoardPage`'s "what's new" tracking, factored out so the loading-race it has to get right
 * is unit-testable on its own: `data` is `undefined` both before the board's first load *and*
 * while a filter combination not yet fetched this session is loading, and seeding the seen
 * set from that empty moment — rather than waiting for `data` to actually arrive — would make
 * every task in the real response look new the instant it lands.
 *
 * The comparison against the previous "seen" set happens inside the effect, not during
 * render: reading `seenIdsRef` while computing this render's output would make the memoized
 * result depend on a mutation React never tracks, and it needs to be exactly that untracked
 * — the entrance delays a card was given must survive every render after the one that
 * started its animation, not get recomputed away the instant the same task is marked "seen".
 * A task's delay is therefore only ever added to state, never removed once its animation
 * has started.
 */
export function useTaskRevealDelays(
  data: readonly { id: string; title: string }[] | undefined,
): Map<string, number> {
  const [revealDelayMs, setRevealDelayMs] = useState(EMPTY_DELAYS);
  const seenIdsRef = useRef<Set<string> | null>(null);

  useEffect(() => {
    if (!data) return;
    const newDelays = computeRevealDelays(data, seenIdsRef.current);
    seenIdsRef.current = new Set(data.map((task) => task.id));
    if (newDelays.size === 0) return;
    setRevealDelayMs((prev) => new Map([...prev, ...newDelays]));
  }, [data]);

  return revealDelayMs;
}

/**
 * Whether `description` just *appeared* (was empty, now isn't) while this hook's caller was
 * already mounted — a project's or a task's description being written while the learner is
 * looking at the page it lives on, not that page's own first load. `undefined` means the
 * project or task hasn't loaded yet, exactly as `useTaskRevealDelays`'s `data` does — and for
 * the same reason: a project or task whose *first* loaded value already has a description
 * (the ordinary case) must not read as "just appeared" only because there was nothing to
 * compare it to yet.
 *
 * A one-shot flag rather than something that can flip back off — once a description's
 * reveal has started, it relies on the same "never undo" rule `useTaskRevealDelays` does, so
 * it survives every render after the one that started it rather than getting recomputed away.
 * Only an *appearance* re-triggers it, not an edit to an already-present description — the
 * same distinction the board draws between a new task and a change to an existing one.
 */
export function useDescriptionReveal(description: string | undefined): boolean {
  const [shouldAnimate, setShouldAnimate] = useState(false);
  const seenRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (description === undefined) return;
    const previous = seenRef.current;
    seenRef.current = description;
    if (previous === undefined) return;
    if (!previous && description) setShouldAnimate(true);
  }, [description]);

  return shouldAnimate;
}
