/**
 * "Research something for this project" — the M8 capability, kicked off from the board
 * rather than from inside a task.
 *
 * docs/03-agent-design.md#research_agent: research with no parent task, about the project
 * as a whole. A collapsed prompt rather than a form that is always open — this is a much
 * rarer action than opening a task, and an always-visible textarea would compete with the
 * board for attention on every visit.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { useStartProjectResearch } from '@/features/queries';
import { ApiError } from '@/lib/api';

export function StartProjectResearch({
  projectId,
  intakeSessionId,
}: {
  projectId: string;
  intakeSessionId: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const navigate = useNavigate();
  const startResearch = useStartProjectResearch(projectId);

  if (!open) {
    return (
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        Research something for this project
      </Button>
    );
  }

  return (
    <form
      className="space-y-2 rounded-lg border p-3"
      onSubmit={(event) => {
        event.preventDefault();
        const trimmed = reason.trim();
        if (!trimmed || !intakeSessionId) return;
        startResearch.mutate(
          { sessionId: intakeSessionId, reason: trimmed },
          {
            onSuccess(run) {
              setOpen(false);
              setReason('');
              navigate(`/projects/${projectId}/research/${run.runId}`);
            },
            onError(error) {
              const detail =
                error instanceof ApiError ? error.problem.detail : 'Could not start research.';
              toast.error(detail || 'Could not start research.');
            },
          },
        );
      }}
    >
      <label className="sr-only" htmlFor="project-research-reason">
        What should your coach research?
      </label>
      <textarea
        id="project-research-reason"
        rows={2}
        value={reason}
        placeholder="What should your coach research? e.g. &ldquo;What's a good first project to build?&rdquo;"
        className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
        onChange={(event) => setReason(event.target.value)}
      />
      <div className="flex justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setOpen(false);
            setReason('');
          }}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          size="sm"
          disabled={startResearch.isPending || reason.trim().length === 0 || !intakeSessionId}
        >
          {startResearch.isPending ? 'Starting…' : 'Research this'}
        </Button>
      </div>
    </form>
  );
}
