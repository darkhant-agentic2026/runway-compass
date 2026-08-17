/**
 * Project list — `/`.
 *
 * docs/06-frontend.md: "cards with progress, open minutes, 'coach updated this' badge".
 * The badge lands with autonomous runs at M5; the counts are live now.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useCreateProject, useProjects } from '@/features/queries'
import { formatMinutes } from '@/lib/format'

export default function ProjectsPage() {
  const projects = useProjects('active')
  const createProject = useCreateProject()
  const [title, setTitle] = useState('')
  const [goal, setGoal] = useState('')

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-4 sm:p-6">
      <h1 className="text-2xl font-semibold">Your projects</h1>

      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault()
          const trimmed = title.trim()
          if (!trimmed) return
          createProject.mutate({ title: trimmed, goal: goal.trim() })
          setTitle('')
          setGoal('')
        }}
      >
        <div className="min-w-40 flex-1">
          <Label htmlFor="new-project-title">New project</Label>
          <Input
            id="new-project-title"
            value={title}
            placeholder="Learn structured concurrency"
            onChange={(event) => setTitle(event.target.value)}
          />
        </div>
        <div className="min-w-40 flex-1">
          <Label htmlFor="new-project-goal">Goal (optional)</Label>
          <Input
            id="new-project-goal"
            value={goal}
            placeholder="Ship a resilient worker pool"
            onChange={(event) => setGoal(event.target.value)}
          />
        </div>
        <Button type="submit" disabled={createProject.isPending || title.trim().length === 0}>
          Create
        </Button>
      </form>

      {projects.isPending ? (
        <p className="text-muted-foreground">Loading your projects…</p>
      ) : (projects.data?.length ?? 0) === 0 ? (
        <p className="text-muted-foreground rounded-lg border border-dashed p-6 text-center">
          No projects yet. Create one above and the coach will help you break it down.
        </p>
      ) : (
        <ul className="space-y-3" data-testid="project-list">
          {projects.data?.map((project) => (
            <li key={project.id}>
              <Card>
                <CardHeader>
                  <CardTitle>
                    <Link className="hover:underline" to={`/projects/${project.id}`}>
                      {project.title}
                    </Link>
                  </CardTitle>
                </CardHeader>
                <CardContent className="text-muted-foreground space-y-1 text-sm">
                  {project.goal ? <p className="text-foreground">{project.goal}</p> : null}
                  <p>
                    {project.counts.completed} of {project.counts.total} tasks done ·{' '}
                    {formatMinutes(project.counts.openMinutes)} of open work
                  </p>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
