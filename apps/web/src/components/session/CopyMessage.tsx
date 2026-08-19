/**
 * Copy a message's source text.
 *
 * The **source**, not the rendered output: a table you copy out of the transcript should
 * paste back as a table, and the learner's own message should come back exactly as they
 * typed it. That second half is what makes rendering their markdown safe at all — the
 * transcript is still the record of what they sent, one click away
 * (`Transcript.tsx`).
 *
 * `navigator.clipboard` is unavailable on an insecure origin and can be refused by
 * permissions policy, so the failure path is real rather than theoretical and says so
 * instead of silently doing nothing.
 */

import { Check, Copy } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/** How long the tick stays before reverting to the copy icon. */
const CONFIRM_MS = 1500;

export function CopyMessage({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A message can be unmounted by a refetch while the tick is showing, and setting state
  // on the way out is a React warning at best and a leak at worst.
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      toast.error('Could not copy — your browser blocked clipboard access.');
      return;
    }
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), CONFIRM_MS);
  }

  return (
    <Button
      variant="ghost"
      size="icon"
      className={cn('size-6 opacity-60 hover:opacity-100', className)}
      // The label changes with the state, so a screen reader hears the confirmation the
      // tick gives everyone else — the icon swap alone announces nothing.
      aria-label={copied ? 'Copied' : 'Copy message'}
      onClick={() => void copy()}
      data-testid="copy-message"
    >
      {copied ? (
        <Check className="size-3.5" aria-hidden="true" />
      ) : (
        <Copy className="size-3.5" aria-hidden="true" />
      )}
    </Button>
  );
}
