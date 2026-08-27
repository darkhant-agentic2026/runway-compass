/**
 * One place for "what kind of material is this" across the app — a `ReportItem`'s, a
 * `ProposedItem`'s, and (since this field was threaded through the promotion end to end,
 * `apps/api/src/coach/services/models.py`'s `TaskItem.kind`) a real checklist item's, all
 * the same five kinds. Consolidated here so `ResearchReport`, `ProposedTaskCard`,
 * `Checklist`, and board `TaskCard`s cannot drift into showing the same kind three
 * different ways.
 *
 * `Globe` is deliberately the icon for `doc` and nothing else has to fall back to it today
 * — every kind this project defines gets its own icon — but it reads as "generic
 * reference material", which is what makes it the right fallback if a future kind ever
 * shows up without one.
 */

import { BookOpen, Code2, Globe, PenLine, Play, type LucideIcon } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { formatMinutes, pluralize } from '@/lib/format';
import type { ReportItemKind } from '@/lib/schemas';
import { cn } from '@/lib/utils';

export const ITEM_KIND_LABELS: Record<ReportItemKind, string> = {
  article: 'Article',
  video: 'Video',
  exercise: 'Exercise',
  doc: 'Docs',
  code_scaffold: 'Scaffold',
};

export const ITEM_KIND_ICONS: Record<ReportItemKind, LucideIcon> = {
  article: BookOpen,
  video: Play,
  exercise: PenLine,
  doc: Globe,
  code_scaffold: Code2,
};

/** One item's kind, inline — icon and label ("Video", "Article", …). Used everywhere an
 * item is listed, so a research report, a proposed task's material, and a real checklist
 * all read the same way. */
export function ItemKindBadge({
  kind,
  className,
}: {
  kind: ReportItemKind;
  className?: string;
}) {
  const Icon = ITEM_KIND_ICONS[kind];
  return (
    <Badge variant="outline" className={cn('text-[0.7rem]', className)} data-testid="item-kind">
      <Icon className="size-3" aria-hidden="true" />
      {ITEM_KIND_LABELS[kind]}
    </Badge>
  );
}

interface KindBearing {
  kind?: ReportItemKind | null;
}

interface KindGroup {
  kind: ReportItemKind;
  count: number;
}

/** `items` grouped by kind, in `ITEM_KIND_LABELS`' declaration order rather than the order
 * items happen to be listed in, so scanning several of these side by side is comparing
 * like with like. */
function groupByKind(items: KindBearing[]): KindGroup[] {
  const counts = new Map<ReportItemKind, number>();
  for (const item of items) {
    if (!item.kind) continue;
    counts.set(item.kind, (counts.get(item.kind) ?? 0) + 1);
  }
  return (Object.keys(ITEM_KIND_LABELS) as ReportItemKind[])
    .filter((kind) => counts.has(kind))
    .map((kind) => ({ kind, count: counts.get(kind) ?? 0 }));
}

function KindGroupIcons({ groups }: { groups: KindGroup[] }) {
  return (
    <>
      {groups.map(({ kind, count }) => {
        const Icon = ITEM_KIND_ICONS[kind];
        return (
          <span
            key={kind}
            className="inline-flex items-center gap-0.5"
            aria-label={pluralize(count, ITEM_KIND_LABELS[kind])}
          >
            <span aria-hidden="true">{count}x</span>
            <Icon className="size-3" aria-hidden="true" />
          </span>
        );
      })}
    </>
  );
}

/**
 * The at-a-glance line beneath a task's title: its estimated duration (when given) and one
 * chip grouping every item's kind by icon and count — "1x[book] 2x[pen]" for an article and
 * two exercises. The count always shows, even at one, so the chip never has to be read two
 * different ways depending on whether something repeats.
 *
 * `required` and `optional` are two groups within the one chip, not one merged count: a
 * required item is a thing the task's size already accounts for, an optional one is a
 * deep-dive the learner may or may not take, and the two are worth telling apart at a
 * glance the same way the task's own material lists do. The required group carries the
 * chip's ordinary styling and a solid underline; the optional group, introduced by a `+`,
 * is very slightly muted and underlined with a dotted line instead — a real board
 * `TaskCard` has no optional items at all (only `required[]` is ever promoted onto a task),
 * so it passes only `required` and the second group never renders.
 */
export function ItemKindStrip({
  minutes,
  required,
  optional = [],
  className,
}: {
  minutes?: number | null;
  required: KindBearing[];
  optional?: KindBearing[];
  className?: string;
}) {
  const requiredGroups = groupByKind(required);
  const optionalGroups = groupByKind(optional);
  const hasGroups = requiredGroups.length > 0 || optionalGroups.length > 0;

  if (!minutes && !hasGroups) return null;

  return (
    <div
      className={cn(
        'flex flex-wrap items-center gap-2 text-xs text-muted-foreground',
        className,
      )}
      data-testid="item-kind-strip"
    >
      {minutes ? <span>{formatMinutes(minutes)}</span> : null}
      {hasGroups ? (
        <Badge
          variant="outline"
          className="h-auto items-center gap-1.5 overflow-visible bg-zinc-200 px-2.5 py-0.5 text-[0.7rem] dark:bg-zinc-700"
          data-testid="item-kind-summary"
        >
          {requiredGroups.length > 0 ? (
            <span
              className="inline-flex items-center gap-1 border-b-2 border-blue-700 pb-0.5 dark:border-blue-400"
              data-testid="item-kind-required"
            >
              <KindGroupIcons groups={requiredGroups} />
            </span>
          ) : null}
          {optionalGroups.length > 0 ? (
            <span
              className="inline-flex items-center gap-1 border-b-2 border-dotted border-blue-700 pb-0.5 opacity-70 dark:border-blue-400"
              data-testid="item-kind-optional"
            >
              <span aria-hidden="true">+</span>
              <KindGroupIcons groups={optionalGroups} />
            </span>
          ) : null}
        </Badge>
      ) : null}
    </div>
  );
}
