/**
 * "Build a roadmap for this project" — `task_proposer` -> `plan_tailor` instead of
 * `reviewer_writer`, so the result is several sized tasks rather than one report.
 *
 * docs/03-agent-design.md#the-taskless-case-task_proposer-and-plan_tailor-replace-reviewer_writer.
 * A sibling of `StartProjectResearch`, not a mode of it: the two hit different endpoints
 * and produce different documents, so keeping them as two collapsed prompts is what makes
 * "which one did I press" legible, where a single form with a dropdown would hide the
 * distinction inside a control easy to leave on the wrong setting.
 */

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { useStartRoadmap } from '@/features/queries';
import { ApiError } from '@/lib/api';

export function StartProjectRoadmap({
  projectId,
  intakeSessionId,
}: {
  projectId: string;
  intakeSessionId: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const navigate = useNavigate();
  const startRoadmap = useStartRoadmap(projectId);

  if (!open) {
    return (
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
        Build a roadmap for this project
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
        startRoadmap.mutate(
          { sessionId: intakeSessionId, reason: trimmed },
          {
            onSuccess(run) {
              setOpen(false);
              setReason('');
              navigate(`/projects/${projectId}/research/${run.runId}`);
            },
            onError(error) {
              const detail =
                error instanceof ApiError
                  ? error.problem.detail
                  : 'Could not start the roadmap.';
              toast.error(detail || 'Could not start the roadmap.');
            },
          },
        );
      }}
    >
      <label className="sr-only" htmlFor="project-roadmap-reason">
        What should the roadmap cover?
      </label>
      <textarea
        id="project-roadmap-reason"
        rows={2}
        value={reason}
        placeholder="What's the goal? e.g. &ldquo;I want to become a data engineer&rdquo;"
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
          disabled={startRoadmap.isPending || reason.trim().length === 0 || !intakeSessionId}
        >
          {startRoadmap.isPending ? 'Starting…' : 'Build the roadmap'}
        </Button>
      </div>
    </form>
  );
}
