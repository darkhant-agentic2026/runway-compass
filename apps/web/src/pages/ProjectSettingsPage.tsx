/**
 * Project preferences — `/projects/:projectId/settings`.
 *
 * docs/06-frontend.md: "Project preferences (task duration, research depth, videos)".
 *
 * Every control shows what it currently resolves to, because a project preference is only
 * meaningful next to the global one it overrides — this is the screen where the brief's
 * "45 minutes globally, 2 hours here" is actually expressed. Clearing a field returns it
 * to inheriting.
 */

import { useParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { useEffectivePrefs, useMe, useProject, usePatchProject } from '@/features/queries'
import { formatMinutes } from '@/lib/format'
import type { ProjectPrefs } from '@/lib/schemas'

const DEPTHS: NonNullable<ProjectPrefs['researchDepth']>[] = ['light', 'standard', 'deep']

export default function ProjectSettingsPage() {
  const { projectId = '' } = useParams()
  const project = useProject(projectId)
  const effective = useEffectivePrefs(projectId)
  const me = useMe()
  const patch = usePatchProject(projectId)

  const prefs = project.data?.prefs
  const globalMinutes = me.data?.globalPrefs.defaultTaskMinutes

  if (!prefs) {
    return <p className="p-6 text-muted-foreground">Loading project settings…</p>
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 p-4 sm:p-6">
      <h1 className="text-2xl font-semibold">{project.data?.title} — settings</h1>

      <Card>
        <CardHeader>
          <CardTitle>Project details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-1">
            <Label htmlFor="project-title">Title</Label>
            <Input
              id="project-title"
              defaultValue={project.data?.title ?? ''}
              onBlur={(event) => {
                const value = event.target.value.trim()
                if (value && value !== project.data?.title) patch.mutate({ title: value })
              }}
            />
          </div>
          <div className="space-y-1">
            <Label htmlFor="project-goal">Goal</Label>
            <Input
              id="project-goal"
              defaultValue={project.data?.goal ?? ''}
              onBlur={(event) => {
                const value = event.target.value.trim()
                if (value !== project.data?.goal) patch.mutate({ goal: value })
              }}
            />
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Preferences for this project</CardTitle>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-1">
            <Label htmlFor="project-minutes">Default task length (minutes)</Label>
            <div className="flex items-center gap-2">
              <Input
                id="project-minutes"
                type="number"
                min={1}
                max={1440}
                placeholder={globalMinutes ? String(globalMinutes) : ''}
                defaultValue={prefs.defaultTaskMinutes ?? ''}
                onBlur={(event) => {
                  const raw = event.target.value.trim()
                  const value = raw === '' ? null : Number(raw)
                  if (value !== prefs.defaultTaskMinutes) {
                    patch.mutate({ prefs: { defaultTaskMinutes: value } })
                  }
                }}
              />
              <Button
                variant="ghost"
                onClick={() => patch.mutate({ prefs: { defaultTaskMinutes: null } })}
                disabled={prefs.defaultTaskMinutes === null}
              >
                Inherit
              </Button>
            </div>
            <p className="text-sm text-muted-foreground" data-testid="minutes-explainer">
              {prefs.defaultTaskMinutes === null
                ? `Inheriting ${globalMinutes ? formatMinutes(globalMinutes) : 'the global default'}`
                : `Overriding the global default${
                    globalMinutes ? ` of ${formatMinutes(globalMinutes)}` : ''
                  }`}
              {effective.data
                ? ` · in effect: ${formatMinutes(effective.data.defaultTaskMinutes)}`
                : ''}
            </p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="research-depth">Research depth</Label>
            <Select
              value={prefs.researchDepth ?? effective.data?.researchDepth ?? 'standard'}
              onValueChange={(value) =>
                patch.mutate({
                  prefs: { researchDepth: value as ProjectPrefs['researchDepth'] },
                })
              }
            >
              <SelectTrigger id="research-depth">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {DEPTHS.map((depth) => (
                  <SelectItem key={depth} value={depth}>
                    {depth}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex items-center gap-2">
            <Switch
              id="allow-videos"
              checked={effective.data?.allowVideos ?? true}
              onCheckedChange={(checked) =>
                patch.mutate({ prefs: { allowVideos: Boolean(checked) } })
              }
            />
            <Label htmlFor="allow-videos" className="font-normal">
              Include videos in research
            </Label>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
