/**
 * The subtask completion ring on a parent card.
 *
 * Colours come from CSS variables (`--progress-track`, `--progress-fill`), never
 * hard-coded hex, so the ring reads correctly in both themes (docs/06-frontend.md).
 * The value is also exposed as text for screen readers — the ring alone carries meaning
 * by colour and shape, which is not enough on its own.
 */

interface ProgressRingProps {
  completed: number;
  total: number;
  size?: number;
}

export function ProgressRing({ completed, total, size = 28 }: ProgressRingProps) {
  const safeTotal = Math.max(total, 1);
  const fraction = Math.min(Math.max(completed / safeTotal, 0), 1);
  const stroke = 3;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`${completed} of ${total} subtasks complete`}
      className="shrink-0"
    >
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--progress-track)"
        strokeWidth={stroke}
      />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={radius}
        fill="none"
        stroke="var(--progress-fill)"
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - fraction)}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
      />
    </svg>
  );
}
